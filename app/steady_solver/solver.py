"""主求解器入口

协调端口图构建、拓扑验证、流量分配、水力/风网求解和设备平衡。
"""

import logging
from dataclasses import dataclass
from typing import Any

from app.calculators.air_network import calculate_air_network
from app.calculators.hydraulic_network import WATER_SYSTEMS, calculate_hydraulic_network
from app.calculators.simulation import simulate_steady_state
from app.data.equipment import capacity_for_device, flow_for_device, role_for_device
from app.schemas import (
    AirNetworkResult,
    DeviceInput,
    EquipmentBalanceResult,
    HydraulicNetworkResult,
    Issue,
    LoadResult,
    SteadyEdgeResult,
    SteadyManifoldResult,
    SteadyMediumResult,
    SteadyNetworkResult,
    SteadyPortResult,
    SystemEvaluateRequest,
)

from .flow_solver import (
    allocate_flow,
    calculate_manifold_residuals,
    check_conservation_tolerance,
)
from .port_graph import Medium, PortGraph, build_port_graph
from .topology_validator import convert_to_schema_issues, validate_topology

logger = logging.getLogger(__name__)


def _load_hardy_cross_allocator() -> Any:
    """延迟导入 Hardy-Cross 流量分配器（不可用则返回 ``None``）。

    **不能在模块顶层导入**。`app.hardy_cross.bridge` 反过来 import
    `app.steady_solver.flow_solver`，而 `app/steady_solver/__init__.py` 会 import
    本模块，于是形成 bridge → steady_solver → solver → bridge 的包级循环。若
    **先**导入 bridge，循环回到本模块时 bridge 只执行到 import 段，
    `allocate_flow_hardy_cross` 尚未定义，顶层导入抛 ImportError。

    原实现用 `try/except ImportError` 吞掉这个异常，后果是**导入顺序决定算法是否
    启用**：`import app.hardy_cross.bridge` 之后再导入本模块，
    `HARDY_CROSS_AVAILABLE` 会静默变成 False，管网平差被无声关掉，结果从"按阻力
    平差"退化成"均分"，评估结果里没有任何痕迹。改为函数内按需导入后，导入顺序
    不再影响可用性；确实导入失败时记一条 warning 而不是静默降级。
    """
    try:
        from app.hardy_cross.bridge import allocate_flow_hardy_cross as allocator
    except ImportError:  # pragma: no cover - 仅在桥接模块真被破坏时触发
        logger.warning("Hardy-Cross 桥接模块不可导入，稳态求解退回均分分配器")
        return None
    return allocator


_UNSET = object()
_hardy_cross_allocator_cache: Any = _UNSET


def _hardy_cross_allocator() -> Any:
    """返回 Hardy-Cross 分配器（不可用时为 ``None``）。

    导入只尝试一次并缓存结果。本函数同时是单元测试的替换接入点——测试直接
    monkeypatch 本函数即可注入伪造的分配器，无需关心缓存与导入顺序。
    """
    global _hardy_cross_allocator_cache
    if _hardy_cross_allocator_cache is _UNSET:
        _hardy_cross_allocator_cache = _load_hardy_cross_allocator()
    return _hardy_cross_allocator_cache


SOURCE_ROLES_BY_MEDIUM: dict[str, set[str]] = {
    "chilled_water": {"cold_source"},
    "cooling_water": {"cold_source"},
    "hot_water": {"heat_source", "cold_source"},
}
SINK_ROLES_BY_MEDIUM: dict[str, set[str]] = {
    "chilled_water": {"terminal"},
    "cooling_water": {"cooling_tower"},
    "hot_water": {"terminal"},
}


@dataclass(frozen=True)
class FlowCapacityResult:
    design_flow_m3_h: float
    actual_flow_m3_h: float
    unmet_flow_m3_h: float
    unmet_load_kw: float | None
    limiting_device_ids: tuple[str, ...]


@dataclass
class SteadySolverResult:
    """稳态求解结果"""
    # 拓扑
    port_graph: PortGraph
    topology_issues: list[Issue]

    # 流量分配
    flow_allocations: dict[str, Any]  # {medium: FlowAllocation}
    conservation_violations: dict[str, list[str]]  # {medium: [device_ids]}

    # 水力结果
    hydraulic_results: HydraulicNetworkResult | None = None

    # 风网结果
    air_results: AirNetworkResult | None = None

    # 设备平衡
    equipment_balance: EquipmentBalanceResult | None = None

    # 系统汇总
    summary: dict[str, Any] | None = None

    # 全局问题
    issues: list[Issue] | None = None


def build_steady_network_result(result: SteadySolverResult) -> SteadyNetworkResult:
    """Convert internal solver dataclasses to the stable API snapshot contract."""
    port_flows: dict[tuple[str, int], dict[str, float]] = {}
    media: dict[str, SteadyMediumResult] = {}

    for medium in sorted(result.port_graph.medium_subgraphs):
        allocation = result.flow_allocations.get(medium)
        subgraph = result.port_graph.medium_subgraphs.get(medium)
        if subgraph is None:
            continue

        if allocation:
            for port_key, flow in allocation.port_flow.items():
                port_flows.setdefault(port_key, {})[medium] = flow

        edge_rows = [
            (edge, allocation.edge_flow.get(index, 0.0) if allocation else 0.0)
            for index, edge in enumerate(subgraph.edges)
        ]
        edge_rows.sort(
            key=lambda item: (
                item[0].pipe_id or "",
                item[0].from_port.device_id,
                item[0].from_port.port_index,
                item[0].to_port.device_id,
                item[0].to_port.port_index,
            )
        )
        edges = [
            SteadyEdgeResult(
                edge_id=f"{medium}:{index}",
                medium=medium,
                from_owner_id=edge.from_port.device_id,
                from_port_index=edge.from_port.port_index,
                to_owner_id=edge.to_port.device_id,
                to_port_index=edge.to_port.port_index,
                pipe_id=edge.pipe_id,
                length_m=edge.length_m,
                diameter_mm=edge.diameter_mm,
                flow_m3_h=flow,
            )
            for index, (edge, flow) in enumerate(edge_rows)
        ]
        residuals = allocation.conservation_residuals if allocation else {}
        manifolds = _build_manifold_results(
            result.port_graph,
            medium,
            allocation,
        )
        max_residual = max(
            max(residuals.values(), default=0.0),
            max((item.residual_m3_h for item in manifolds), default=0.0),
        )
        topology_failed = any(
            issue.level == "error" and issue.target == f"{medium}_network"
            for issue in result.topology_issues
        )
        total_demand = allocation.total_demand if allocation else 0.0
        actual_flow = (
            allocation.actual_flow_m3_h
            if allocation and allocation.actual_flow_m3_h is not None
            else total_demand
        )
        media[medium] = SteadyMediumResult(
            total_demand_m3_h=total_demand,
            actual_flow_m3_h=actual_flow,
            unmet_flow_m3_h=allocation.unmet_flow_m3_h if allocation else 0.0,
            unmet_load_kw=allocation.unmet_load_kw if allocation else None,
            limiting_device_ids=list(allocation.limiting_device_ids) if allocation else [],
            converged=not topology_failed and not result.conservation_violations.get(medium),
            max_conservation_residual_m3_h=max_residual,
            conservation_residuals_m3_h=dict(sorted(residuals.items())),
            edges=edges,
            manifolds=manifolds,
        )

    ports = [
        SteadyPortResult(
            owner_id=port.device_id,
            owner_kind=port.owner_kind,
            port_index=port.port_index,
            direction=port.direction,
            medium=port.medium,
            diameter_mm=port.diameter_mm,
            floor_id=port.floor_id,
            is_valid=port.is_valid,
            flow_m3_h_by_medium=dict(
                sorted(port_flows.get((port.device_id, port.port_index), {}).items())
            ),
        )
        for port in sorted(
            result.port_graph.ports,
            key=lambda item: (item.owner_kind, item.device_id, item.port_index),
        )
    ]

    return SteadyNetworkResult(
        ports=ports,
        media=media,
        hydraulic=result.hydraulic_results,
        air=result.air_results,
        equipment=result.equipment_balance,
        summary=result.summary or {},
        issues=result.issues or [],
    )


def _build_manifold_results(
    graph: PortGraph,
    medium: Medium,
    allocation: Any | None,
) -> list[SteadyManifoldResult]:
    bindings = [
        binding
        for binding in graph.manifold_bindings
        if binding.medium == medium
    ]
    residuals = (
        calculate_manifold_residuals(graph, medium, allocation)
        if allocation
        else {}
    )
    results: list[SteadyManifoldResult] = []

    for manifold_id in sorted({binding.manifold_id for binding in bindings}):
        manifold_bindings = sorted(
            (
                binding
                for binding in bindings
                if binding.manifold_id == manifold_id
            ),
            key=lambda item: (item.port_index, item.peer_id),
        )
        port_bindings = {
            binding.peer_id: binding.port_index
            for binding in manifold_bindings
        }
        branch_ports = {
            binding.peer_id: binding.port_index
            for binding in manifold_bindings
            if binding.kind == "branch"
        }
        main_flow = round(
            allocation.port_flow.get((manifold_id, 0), 0.0)
            if allocation
            else 0.0,
            3,
        )
        residual = round(residuals.get(manifold_id, 0.0), 3)
        branch_flows = {
            peer_id: round(
                allocation.port_flow.get((manifold_id, port_index), 0.0)
                if allocation
                else 0.0,
                3,
            )
            for peer_id, port_index in sorted(branch_ports.items())
        }
        if branch_flows and residual == 0.0:
            last_peer_id = next(reversed(branch_flows))
            branch_flows[last_peer_id] = round(
                main_flow
                - sum(
                    flow
                    for peer_id, flow in branch_flows.items()
                    if peer_id != last_peer_id
                ),
                3,
            )
        results.append(
            SteadyManifoldResult(
                manifold_id=manifold_id,
                main_flow_m3_h=main_flow,
                branch_flows_m3_h=dict(sorted(branch_flows.items())),
                residual_m3_h=residual,
                port_bindings=dict(sorted(port_bindings.items())),
            )
        )

    return results


def _build_terminal_demands(
    request: SystemEvaluateRequest,
    graph: PortGraph,
    medium: Medium,
    total_flow_m3_h: float,
) -> dict[str, float]:
    """Build a balanced, deterministic source/sink demand map for one medium."""
    subgraph = graph.medium_subgraphs.get(medium)
    if total_flow_m3_h <= 0 or not subgraph or not subgraph.edges:
        return {}

    devices_by_id = {device.id: device for device in request.devices}
    medium_devices = [
        devices_by_id[device_id]
        for device_id in sorted(subgraph.device_ports)
        if device_id in devices_by_id
    ]

    sources = [
        device
        for device in medium_devices
        if _steady_solver_role(device) in SOURCE_ROLES_BY_MEDIUM.get(medium, set())
    ]
    sinks = [
        device
        for device in medium_devices
        if _steady_solver_role(device) in SINK_ROLES_BY_MEDIUM.get(medium, set())
    ]
    if not sources or not sinks:
        return {}

    demands = _split_flow(sources, total_flow_m3_h, medium)
    demands.update(_split_flow(sinks, -total_flow_m3_h, medium))
    return demands


def _steady_solver_role(device: DeviceInput) -> str | None:
    """Return the catalog role, with compatibility aliases for legacy DTOs."""
    catalog_role = role_for_device(device)
    if catalog_role:
        return catalog_role

    device_type = device.type.strip().lower()
    if device_type in {"chiller", "cold_source", "冷源机组", "冷水机组"}:
        return "cold_source"
    if device_type in {"heat_source", "boiler", "heat_pump", "热源机组", "锅炉", "热泵"}:
        return "heat_source"
    if device_type in {"cooling_tower", "冷却塔"}:
        return "cooling_tower"
    if device_type in {
        "terminal",
        "fcu",
        "ahu",
        "fresh_air_unit",
        "风机盘管",
        "组合式空调机组",
        "新风机组",
    }:
        return "terminal"
    if device_type in {"pump", "循环泵"}:
        return "pump"
    return None


def _split_flow(
    devices: list[DeviceInput],
    total_flow_m3_h: float,
    medium: Medium,
) -> dict[str, float]:
    """Split flow by rated flow/capacity while keeping the exact total."""
    ordered_devices = sorted(devices, key=lambda device: device.id)
    weights = [
        (
            device.flow_m3_h * device.quantity
            if device.flow_m3_h is not None
            else device.capacity_kw * device.quantity
            if device.capacity_kw is not None
            else 0.0
        )
        or flow_for_device(device, medium)
        or capacity_for_device(device)
        or float(device.quantity)
        for device in ordered_devices
    ]
    weight_total = sum(weights)

    allocations: dict[str, float] = {}
    allocated = 0.0
    for device, weight in zip(ordered_devices[:-1], weights[:-1]):
        share = total_flow_m3_h * weight / weight_total
        allocations[device.id] = share
        allocated += share
    allocations[ordered_devices[-1].id] = total_flow_m3_h - allocated
    return allocations


def _limit_flow_by_equipment(
    request: SystemEvaluateRequest,
    graph: PortGraph,
    medium: Medium,
    design_flow_m3_h: float,
) -> FlowCapacityResult:
    """Limit one medium by connected source, pump, and sink rated capacity."""
    subgraph = graph.medium_subgraphs.get(medium)
    if not subgraph or design_flow_m3_h <= 0:
        return FlowCapacityResult(
            design_flow_m3_h=design_flow_m3_h,
            actual_flow_m3_h=design_flow_m3_h,
            unmet_flow_m3_h=0.0,
            unmet_load_kw=_unmet_load_for_flow(medium, 0.0),
            limiting_device_ids=(),
        )

    devices_by_id = {device.id: device for device in request.devices}
    medium_devices = [
        devices_by_id[device_id]
        for device_id in sorted(subgraph.device_ports)
        if device_id in devices_by_id
    ]
    groups = [
        [
            device
            for device in medium_devices
            if _steady_solver_role(device)
            in SOURCE_ROLES_BY_MEDIUM.get(medium, set())
        ],
        [
            device
            for device in medium_devices
            if _steady_solver_role(device) == "pump"
        ],
        [
            device
            for device in medium_devices
            if _steady_solver_role(device)
            in SINK_ROLES_BY_MEDIUM.get(medium, set())
        ],
    ]
    limits = [
        group_limit
        for devices in groups
        if (group_limit := _aggregate_flow_limit(devices, medium)) is not None
    ]
    actual_flow = min(
        [design_flow_m3_h, *(flow_limit for flow_limit, _ids in limits)]
    )
    unmet_flow = max(design_flow_m3_h - actual_flow, 0.0)
    limiting_ids = (
        tuple(
            sorted(
                {
                    device_id
                    for flow_limit, device_ids in limits
                    if abs(flow_limit - actual_flow) <= 0.001
                    for device_id in device_ids
                }
            )
        )
        if unmet_flow > 0.001
        else ()
    )
    return FlowCapacityResult(
        design_flow_m3_h=design_flow_m3_h,
        actual_flow_m3_h=actual_flow,
        unmet_flow_m3_h=unmet_flow,
        unmet_load_kw=_unmet_load_for_flow(medium, unmet_flow),
        limiting_device_ids=limiting_ids,
    )


def _aggregate_flow_limit(
    devices: list[DeviceInput],
    medium: Medium,
) -> tuple[float, tuple[str, ...]] | None:
    """Return an aggregate group limit only when every device is rated."""
    if not devices:
        return None

    device_limits = [
        (device, _device_flow_limit(device, medium))
        for device in sorted(devices, key=lambda item: item.id)
    ]
    if any(flow_limit <= 0 for _device, flow_limit in device_limits):
        return None
    return (
        sum(flow_limit for _device, flow_limit in device_limits),
        tuple(device.id for device, _flow_limit in device_limits),
    )


def _device_flow_limit(device: DeviceInput, medium: Medium) -> float:
    rated_flow = flow_for_device(device, medium)
    role = _steady_solver_role(device)
    capacity_flow = (
        _flow_for_capacity(capacity_for_device(device), medium)
        if role
        in SOURCE_ROLES_BY_MEDIUM.get(medium, set())
        | SINK_ROLES_BY_MEDIUM.get(medium, set())
        else 0.0
    )
    limits = [value for value in (rated_flow, capacity_flow) if value > 0]
    return min(limits, default=0.0)


def _flow_for_capacity(capacity_kw: float, medium: Medium) -> float:
    if capacity_kw <= 0:
        return 0.0
    if medium == "chilled_water":
        return 0.86 * capacity_kw / 5.0
    if medium == "cooling_water":
        return 0.86 * capacity_kw / 5.0 * 1.2
    if medium == "hot_water":
        return 0.86 * capacity_kw / 10.0
    return 0.0


def _unmet_load_for_flow(medium: Medium, unmet_flow_m3_h: float) -> float | None:
    if medium == "chilled_water":
        return unmet_flow_m3_h * 5.0 / 0.86
    if medium == "hot_water":
        return unmet_flow_m3_h * 10.0 / 0.86
    return None


def _normalized_solver_request(request: SystemEvaluateRequest) -> SystemEvaluateRequest:
    """Normalize collection order at the solver boundary for reproducible output."""
    return request.model_copy(
        update={
            "floors": sorted(request.floors, key=lambda item: item.id),
            "rooms": sorted(request.rooms, key=lambda item: (item.id or "", item.name)),
            "devices": sorted(request.devices, key=lambda item: item.id),
            "pipes": sorted(request.pipes, key=lambda item: item.id),
            "connections": sorted(
                request.connections,
                key=lambda item: (item.system, item.from_id, item.to_id),
            ),
        }
    )


def _build_equipment_balance(
    request: SystemEvaluateRequest,
    cooling_load_kw: float,
    heating_load_kw: float,
    hydraulic: HydraulicNetworkResult | None,
    air: AirNetworkResult | None,
) -> EquipmentBalanceResult:
    cold_source_capacity = sum(
        capacity_for_device(device)
        for device in request.devices
        if _steady_solver_role(device) == "cold_source"
    )
    heat_source_capacity = sum(
        capacity_for_device(device)
        for device in request.devices
        if _steady_solver_role(device) == "heat_source"
    )
    terminal_capacity = sum(
        capacity_for_device(device)
        for device in request.devices
        if _steady_solver_role(device) == "terminal"
    )

    cooling_delivered = min(cooling_load_kw, cold_source_capacity, terminal_capacity)
    heating_delivered = min(heating_load_kw, heat_source_capacity, terminal_capacity)
    cooling_residual = max(cooling_load_kw - cooling_delivered, 0.0)
    heating_residual = max(heating_load_kw - heating_delivered, 0.0)

    load = LoadResult(
        total_cooling_kw=cooling_load_kw,
        total_heating_kw=heating_load_kw,
        adjusted_cooling_kw=cooling_load_kw,
        adjusted_heating_kw=heating_load_kw,
        rooms=[],
    )
    simulation = simulate_steady_state(request, load, hydraulic, air)
    issues = list(simulation.issues)
    if cooling_residual > 0.01:
        issues.append(
            Issue(
                level="warning",
                code="energy_balance_violation",
                message=(
                    "Cooling source/terminal delivery is below the requested "
                    f"load by {cooling_residual:.3f} kW."
                ),
                target="cooling_system",
            )
        )
    if heating_residual > 0.01:
        issues.append(
            Issue(
                level="warning",
                code="energy_balance_violation",
                message=(
                    "Heating source/terminal delivery is below the requested "
                    f"load by {heating_residual:.3f} kW."
                ),
                target="heating_system",
            )
        )

    return EquipmentBalanceResult(
        cooling_load_kw=cooling_load_kw,
        heating_load_kw=heating_load_kw,
        cold_source_capacity_kw=cold_source_capacity,
        heat_source_capacity_kw=heat_source_capacity,
        terminal_capacity_kw=terminal_capacity,
        cooling_delivered_kw=cooling_delivered,
        heating_delivered_kw=heating_delivered,
        cooling_balance_residual_kw=cooling_residual,
        heating_balance_residual_kw=heating_residual,
        simulation=simulation,
        issues=issues,
    )


def solve_steady_state(
    request: SystemEvaluateRequest,
    cooling_load_kw: float = 0.0,
    heating_load_kw: float = 0.0,
    conservation_tolerance: float = 0.01,
    use_hardy_cross: bool = True,
) -> SteadySolverResult:
    """稳态管网与设备求解器主入口

    Args:
        request: 系统评估请求
        cooling_load_kw: 冷负荷（kW）
        heating_load_kw: 热负荷（kW）
        conservation_tolerance: 守恒残差容差（m³/h）
        use_hardy_cross: 是否使用 Hardy Cross 方法（默认 True）

    Returns:
        SteadySolverResult 包含拓扑、流量、水力、风网和设备平衡结果
    """
    request = _normalized_solver_request(request)

    # 1. 构建端口图
    port_graph = build_port_graph(request)

    # 2. 拓扑验证
    topology_issues_raw = validate_topology(port_graph)
    topology_issues = convert_to_schema_issues(topology_issues_raw)

    # 3. 流量分配（每个介质独立）
    flow_allocations: dict[str, Any] = {}
    conservation_violations: dict[str, list[str]] = {}
    flow_capacity_issues: list[Issue] = []

    def _allocate(medium: Medium, flow_m3_h: float) -> Any:
        """按介质分配流量，优先 Hardy Cross，缺少源/汇时回退到均分求解器。"""
        capacity = _limit_flow_by_equipment(
            request,
            port_graph,
            medium,
            flow_m3_h,
        )
        demands = _build_terminal_demands(
            request,
            port_graph,
            medium,
            capacity.actual_flow_m3_h,
        )
        if use_hardy_cross and demands:
            hardy_cross_allocator = _hardy_cross_allocator()
        else:
            hardy_cross_allocator = None
        if hardy_cross_allocator is not None:
            allocation = hardy_cross_allocator(
                port_graph,
                medium,
                capacity.actual_flow_m3_h,
                demands,
            )
        else:
            allocation = allocate_flow(
                port_graph,
                medium,
                capacity.actual_flow_m3_h,
            )

        allocation.total_demand = capacity.design_flow_m3_h
        allocation.actual_flow_m3_h = capacity.actual_flow_m3_h
        allocation.unmet_flow_m3_h = capacity.unmet_flow_m3_h
        allocation.unmet_load_kw = capacity.unmet_load_kw
        allocation.limiting_device_ids = capacity.limiting_device_ids
        # 降级透明：Hardy-Cross 结果数值不可信时已回退到均分求解器（见
        # `hardy_cross.bridge.allocate_flow_hardy_cross`），这里必须显式告知，
        # 否则使用者会以为这份流量是平差结果。
        if getattr(allocation, "degraded_reason", None):
            flow_capacity_issues.append(
                Issue(
                    level="warning",
                    code="HARDY_CROSS_FALLBACK",
                    message=(
                        f"{medium} 的 Hardy-Cross 平差结果数值不可信，已回退到按需求"
                        f"路由的兜底分配器（{allocation.degraded_reason}）。"
                        "流量守恒仍然成立，但未经管网阻力平差，支路流量仅供教学参考，"
                        "不能用于阻力平衡或选型结论。"
                    ),
                    target=f"{medium}_network",
                )
            )
        if capacity.unmet_flow_m3_h > 0.001:
            load_detail = (
                f"，未满足负荷 {capacity.unmet_load_kw:.3f} kW"
                if capacity.unmet_load_kw is not None
                else ""
            )
            flow_capacity_issues.append(
                Issue(
                    level="warning",
                    code="flow_capacity_limited",
                    message=(
                        f"{medium} 设计流量 {capacity.design_flow_m3_h:.3f} m³/h "
                        f"受设备能力限制为 {capacity.actual_flow_m3_h:.3f} m³/h"
                        f"{load_detail}"
                    ),
                    target=f"{medium}_network",
                )
            )

        for manifold_id, residual in calculate_manifold_residuals(
            port_graph,
            medium,
            allocation,
        ).items():
            allocation.conservation_residuals[manifold_id] = max(
                allocation.conservation_residuals.get(manifold_id, 0.0),
                residual,
            )
        return allocation

    # 冷冻水流量（基于冷负荷）
    if cooling_load_kw > 0:
        chilled_flow_m3_h = 0.86 * cooling_load_kw / 5.0  # ΔT=5°C
        chilled_allocation = _allocate("chilled_water", chilled_flow_m3_h)
        flow_allocations["chilled_water"] = chilled_allocation
        violations = check_conservation_tolerance(chilled_allocation.conservation_residuals, conservation_tolerance)
        if violations:
            conservation_violations["chilled_water"] = violations

    # 冷却水流量
    if cooling_load_kw > 0:
        cooling_flow_m3_h = 0.86 * cooling_load_kw / 5.0 * 1.2  # 冷却水 = 冷冻水 × 1.2
        cooling_allocation = _allocate("cooling_water", cooling_flow_m3_h)
        flow_allocations["cooling_water"] = cooling_allocation
        violations = check_conservation_tolerance(cooling_allocation.conservation_residuals, conservation_tolerance)
        if violations:
            conservation_violations["cooling_water"] = violations

    # 热水流量
    if heating_load_kw > 0:
        hot_flow_m3_h = 0.86 * heating_load_kw / 10.0  # ΔT=10°C
        hot_allocation = _allocate("hot_water", hot_flow_m3_h)
        flow_allocations["hot_water"] = hot_allocation
        violations = check_conservation_tolerance(hot_allocation.conservation_residuals, conservation_tolerance)
        if violations:
            conservation_violations["hot_water"] = violations

    # 5. 复用现有物理网络计算器，保持独立 API 与稳态管线结果一致。
    hydraulic_results = (
        calculate_hydraulic_network(request, cooling_load_kw, heating_load_kw)
        if any(pipe.system in WATER_SYSTEMS for pipe in request.pipes)
        else None
    )
    air_results = (
        calculate_air_network(request, cooling_load_kw)
        if any(pipe.system == "air" for pipe in request.pipes)
        else None
    )

    # 6. 设备额定能力与部分负荷能耗平衡。
    equipment_balance = None
    if cooling_load_kw > 0 or heating_load_kw > 0:
        equipment_balance = _build_equipment_balance(
            request,
            cooling_load_kw,
            heating_load_kw,
            hydraulic_results,
            air_results,
        )

    # 7. 系统汇总
    summary = {
        "port_count": len(port_graph.ports),
        "edge_count": len(port_graph.edges),
        "medium_subgraphs": {medium: len(sub.edges) for medium, sub in port_graph.medium_subgraphs.items()},
        "topology_issue_count": len(topology_issues),
        "conservation_violation_count": sum(len(v) for v in conservation_violations.values()),
        "required_pump_head_m": hydraulic_results.required_pump_head_m if hydraulic_results else None,
        "required_fan_pressure_pa": air_results.required_fan_pressure_pa if air_results else None,
        "total_energy_kw": (
            equipment_balance.simulation.total_energy_kw if equipment_balance else None
        ),
        "cooling_balance_residual_kw": (
            equipment_balance.cooling_balance_residual_kw if equipment_balance else None
        ),
        "heating_balance_residual_kw": (
            equipment_balance.heating_balance_residual_kw if equipment_balance else None
        ),
    }

    # 8. 收集全局问题
    issues = list(topology_issues)
    issues.extend(flow_capacity_issues)
    if hydraulic_results:
        issues.extend(hydraulic_results.issues)
    if air_results:
        issues.extend(air_results.issues)
    if equipment_balance:
        issues.extend(equipment_balance.issues)
    for medium, device_ids in conservation_violations.items():
        for device_id in device_ids:
            issues.append(Issue(
                level="error",
                code="conservation_violation",
                message=f"{medium} 系统设备 {device_id} 流量守恒违反（残差 > {conservation_tolerance}）",
                target=device_id,
            ))

    return SteadySolverResult(
        port_graph=port_graph,
        topology_issues=topology_issues,
        flow_allocations=flow_allocations,
        conservation_violations=conservation_violations,
        hydraulic_results=hydraulic_results,
        air_results=air_results,
        equipment_balance=equipment_balance,
        summary=summary,
        issues=issues,
    )
