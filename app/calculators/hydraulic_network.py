from collections import defaultdict
from typing import Literal

from app.building import (
    floor_base_summary,
    implicit_floor_id,
    node_floor_id,
    pipe_floor_id,
)
from app.calculators.network_paths import (
    enumerate_network_paths,
    is_closed_circuit,
    pipe_ids_in_path,
)
from app.calculators.topology import analyze_topology
from app.calculators.water import (
    GRAVITY_M_S2,
    WATER_DENSITY_KG_M3,
    calculate_water_from_flow,
)
from app.data.equipment import (
    catalog_number_for_device,
    flow_for_device,
    head_for_device,
    role_for_device,
)
from app.data.rules import rule_value
from app.schemas import (
    HydraulicNetworkResult,
    Issue,
    NetworkPathCalculation,
    PipeCalculation,
    PumpCheckResult,
    SystemEvaluateRequest,
)

WATER_SYSTEMS = ("chilled_water", "cooling_water", "hot_water")


def calculate_hydraulic_network(
    request: SystemEvaluateRequest,
    cooling_load_kw: float = 0.0,
    heating_load_kw: float = 0.0,
) -> HydraulicNetworkResult:
    water_pipes = [pipe for pipe in request.pipes if pipe.system in WATER_SYSTEMS]
    issues: list[Issue] = []
    total_flow_by_system = _required_flow_by_system(cooling_load_kw, heating_load_kw)
    topology = analyze_topology(request, total_flow_by_system)
    pipes_by_system = {system: [pipe for pipe in water_pipes if pipe.system == system] for system in WATER_SYSTEMS}
    calculations: list[PipeCalculation] = []
    # 设备阻力先一次性归到单根管段上，避免串联设备被供、回水管各计一次。
    device_resistance = _water_resistance_by_pipe(request)

    for system, pipes in pipes_by_system.items():
        if not pipes:
            continue
        base_flow = total_flow_by_system.get(system, 0.0) or sum(pipe.flow_m3_h or 0.0 for pipe in pipes)
        if base_flow <= 0:
            base_flow = float(len(pipes))
        for pipe in pipes:
            flow_m3_h = topology.pipe_flow_m3_h.get(pipe.id) or pipe.flow_m3_h or base_flow / max(len(pipes), 1)
            connected_pressure_pa, connected_local_k = device_resistance.get(pipe.id, (0.0, 0.0))
            result = calculate_water_from_flow(
                flow_m3_h=flow_m3_h,
                length_m=pipe.length_m,
                local_resistance_coefficient=pipe.local_resistance_coefficient + connected_local_k,
                diameter_mm=pipe.diameter_mm,
                system=pipe.system,
            )
            pressure_drop_pa = result.pressure_drop_pa + connected_pressure_pa
            pump_head_m = pressure_drop_pa / (WATER_DENSITY_KG_M3 * GRAVITY_M_S2) * 1.1
            issues.extend(_retarget_issues(result.issues, pipe.id))
            calculations.append(
                PipeCalculation(
                    pipe_id=pipe.id,
                    system=pipe.system,
                    flow_m3_h=result.flow_m3_h,
                    size=result.selected_pipe,
                    velocity_m_s=result.actual_velocity_m_s,
                    pressure_drop_pa=round(pressure_drop_pa, 3),
                    pump_head_m=round(pump_head_m, 3),
                )
            )

    path_calculations = _path_calculations(request, calculations)
    required_pump_head_m = _required_head_from_paths(calculations, path_calculations)
    most_unfavorable = max(calculations, key=lambda item: item.pressure_drop_pa, default=None)
    most_unfavorable_path = max(path_calculations, key=lambda item: item.pressure_drop_pa, default=None)
    pump_check = _build_pump_check(request, total_flow_by_system.get("chilled_water", 0.0), required_pump_head_m)
    issues.extend(_issues_from_pump_check(pump_check))
    issues.extend(_issues_from_circuits(request, path_calculations))
    balancing = _balancing_summary(path_calculations)
    issues.extend(_issues_from_balancing(balancing))
    issues.extend(topology.issues)
    floor_flow_summary = _floor_flow_summary(request, calculations)

    return HydraulicNetworkResult(
        total_flow_by_system_m3_h={key: round(value, 3) for key, value in total_flow_by_system.items() if value > 0},
        pipes=calculations,
        paths=path_calculations,
        most_unfavorable_pipe_id=most_unfavorable.pipe_id if most_unfavorable else None,
        most_unfavorable_path_id=most_unfavorable_path.path_id if most_unfavorable_path else None,
        required_pump_head_m=required_pump_head_m,
        pump_check=pump_check,
        balancing=balancing,
        floor_flow_summary=floor_flow_summary,
        issues=_dedupe_issues(issues),
    )


def _required_flow_by_system(cooling_load_kw: float, heating_load_kw: float) -> dict[str, float]:
    chilled = 0.86 * cooling_load_kw / 5.0 if cooling_load_kw > 0 else 0.0
    return {
        "chilled_water": chilled,
        "cooling_water": chilled * 1.2 if chilled > 0 else 0.0,
        "hot_water": 0.86 * heating_load_kw / 10.0 if heating_load_kw > 0 else 0.0,
    }


def _build_pump_check(request: SystemEvaluateRequest, required_flow_m3_h: float, required_head_m: float) -> PumpCheckResult | None:
    pumps = [device for device in request.devices if role_for_device(device) == "pump" or device.type == "循环泵"]
    if not pumps or required_flow_m3_h <= 0:
        return None

    total_pump_flow = sum(flow_for_device(pump) for pump in pumps)
    max_pump_head = max((head_for_device(pump) for pump in pumps), default=0.0)
    duty_count = sum(pump.quantity for pump in pumps)
    standby_count = int(request.design_options.get("standby_pump_count", 0))
    flow_margin = total_pump_flow / required_flow_m3_h if required_flow_m3_h > 0 and total_pump_flow > 0 else None
    head_margin = max_pump_head / required_head_m if required_head_m > 0 and max_pump_head > 0 else None
    status: Literal["ok", "warning", "error"] = "ok"
    if (flow_margin is not None and flow_margin < float(rule_value("pump_margin.flow_min", 1.0))) or (head_margin is not None and head_margin < float(rule_value("pump_margin.head_min", 1.05))):
        status = "warning"
    if total_pump_flow <= 0 or max_pump_head <= 0:
        status = "error"
    return PumpCheckResult(
        required_flow_m3_h=round(required_flow_m3_h, 3),
        installed_flow_m3_h=round(total_pump_flow, 3),
        required_head_m=round(required_head_m, 3),
        available_head_m=round(max_pump_head, 3),
        duty_pump_count=duty_count,
        standby_pump_count=standby_count,
        flow_margin=round(flow_margin, 3) if flow_margin is not None else None,
        head_margin=round(head_margin, 3) if head_margin is not None else None,
        status=status,
    )


# 水侧阻力计入的设备角色。泵是**加压**部件，它的压降不是环路损失，
# 若计入等于让泵自己抵消自己的扬程。
RESISTANCE_ROLES = {"water_accessory", "terminal", "cold_source", "heat_source", "cooling_tower"}


def _water_resistance_by_pipe(request: SystemEvaluateRequest) -> dict[str, tuple[float, float]]:
    """把设备水侧阻力归到**一根**相邻管段上

    串联设备（正常接法）同时挨着供水管和回水管。原先按"逐根相邻管各加一次"
    摊派，环路把两根管一起累加时，同一台设备的压降就被算了两遍。
    这里每台设备在每个系统里只挂到一根管上（按 id 排序取首根，保证确定性），
    环路求和即为真实值。
    """
    devices = {device.id: device for device in request.devices}
    pipe_systems = {pipe.id: pipe.system for pipe in request.pipes}

    adjacent: defaultdict[tuple[str, str], list[str]] = defaultdict(list)
    for connection in request.connections:
        for node, other in ((connection.from_id, connection.to_id), (connection.to_id, connection.from_id)):
            if node not in devices or other not in pipe_systems:
                continue
            if pipe_systems[other] != connection.system:
                continue
            if other not in adjacent[(node, connection.system)]:
                adjacent[(node, connection.system)].append(other)

    resistance: dict[str, tuple[float, float]] = {}
    for (device_id, _system), pipe_ids in adjacent.items():
        device = devices[device_id]
        if role_for_device(device) not in RESISTANCE_ROLES:
            continue
        pressure_pa = catalog_number_for_device(device, "water_pressure_drop_kpa", 0.0) * 1000.0
        pressure_pa += catalog_number_for_device(device, "pressure_drop_pa", 0.0)
        local_k = catalog_number_for_device(device, "local_resistance_coefficient", 0.0)
        if pressure_pa <= 0.0 and local_k <= 0.0:
            continue
        # pipe_ids 由 defaultdict(list) 的 .append 唯一构造，且此处遍历的是
        # 已存在的键，故必非空，min() 与 sorted()[0] 等价。
        target = min(pipe_ids)
        previous = resistance.get(target, (0.0, 0.0))
        resistance[target] = (previous[0] + pressure_pa, previous[1] + local_k)
    return resistance


def _issues_from_pump_check(check: PumpCheckResult | None) -> list[Issue]:
    if not check:
        return []
    flow_min = float(rule_value("pump_margin.flow_min", 1.0))
    flow_max = float(rule_value("pump_margin.flow_max", 1.25))
    head_min = float(rule_value("pump_margin.head_min", 1.05))
    head_max = float(rule_value("pump_margin.head_max", 1.3))
    issues: list[Issue] = []

    if check.installed_flow_m3_h <= 0:
        issues.append(Issue(level="error", code="PUMP_FLOW_UNKNOWN", message="Pump catalog has no rated flow data."))
    elif check.flow_margin is not None and check.flow_margin < flow_min:
        issues.append(Issue(level="warning", code="PUMP_FLOW_LOW", message="Pump flow is below required chilled-water flow."))
    elif check.flow_margin is not None and check.flow_margin > flow_max:
        issues.append(Issue(level="info", code="PUMP_FLOW_HIGH", message="Pump flow margin is higher than the recommended range."))
    if check.available_head_m <= 0:
        issues.append(Issue(level="error", code="PUMP_HEAD_UNKNOWN", message="Pump catalog has no rated head data."))
    elif check.head_margin is not None and check.head_margin < head_min:
        issues.append(Issue(level="warning", code="PUMP_HEAD_LOW", message="Pump head is below calculated network head."))
    elif check.head_margin is not None and check.head_margin > head_max:
        issues.append(Issue(level="info", code="PUMP_HEAD_HIGH", message="Pump head margin is higher than the recommended range."))
    return issues


def _path_calculations(request: SystemEvaluateRequest, calculations: list[PipeCalculation]) -> list[NetworkPathCalculation]:
    by_pipe = {item.pipe_id: item for item in calculations}
    path_items: list[NetworkPathCalculation] = []
    for system in WATER_SYSTEMS:
        for index, nodes in enumerate(enumerate_network_paths(request, system), start=1):
            pipe_ids = pipe_ids_in_path(request, nodes, system)
            pressure_drop = sum(by_pipe[pipe_id].pressure_drop_pa for pipe_id in pipe_ids if pipe_id in by_pipe)
            head_m = pressure_drop / (WATER_DENSITY_KG_M3 * GRAVITY_M_S2) * 1.1 if pressure_drop > 0 else 0.0
            path_flow = min((by_pipe[pipe_id].flow_m3_h for pipe_id in pipe_ids if pipe_id in by_pipe), default=0.0)
            path_items.append(
                NetworkPathCalculation(
                    path_id=f"{system}_path_{index}",
                    system=system,
                    nodes=nodes,
                    floor_ids=_path_floor_ids(request, nodes),
                    pipe_ids=pipe_ids,
                    pressure_drop_pa=round(pressure_drop, 3),
                    head_m=round(head_m, 3),
                    flow_m3_h=round(path_flow, 3),
                    complete=bool(pipe_ids),
                    closed_circuit=is_closed_circuit(request, nodes, system),
                )
            )
    return path_items


def _floor_flow_summary(request: SystemEvaluateRequest, calculations: list[PipeCalculation]) -> dict[str, dict[str, float]]:
    summary = {
        floor_id: {"chilled_water": 0.0, "cooling_water": 0.0, "hot_water": 0.0, "pipe_count": 0}
        for floor_id in floor_base_summary(request)
    }
    pipe_map = {pipe.id: pipe for pipe in request.pipes}
    for calc in calculations:
        pipe = pipe_map.get(calc.pipe_id)
        if not pipe:
            continue
        floor_id = pipe_floor_id(pipe, implicit=implicit_floor_id(request))
        item = summary.setdefault(floor_id, {"chilled_water": 0.0, "cooling_water": 0.0, "hot_water": 0.0, "pipe_count": 0})
        item[calc.system] = round(item.get(calc.system, 0.0) + calc.flow_m3_h, 3)
        item["pipe_count"] += 1
    return summary


def _path_floor_ids(request: SystemEvaluateRequest, nodes: list[str]) -> list[str]:
    floor_ids = []
    for node in nodes:
        floor_id = node_floor_id(request, node)
        if floor_id not in floor_ids:
            floor_ids.append(floor_id)
    return floor_ids


def _required_head_from_paths(calculations: list[PipeCalculation], paths: list[NetworkPathCalculation]) -> float:
    """水泵扬程 = 最不利**闭合环路**的阻力

    只有闭合环路同时含供程与回程。退而用单管程路径的 max 会漏掉回程，
    扬程偏小近一半，泵选型不足；故环路不闭合时改用全部冷冻水管之和兜底
    （偏保守），并由 `_issues_from_circuits` 如实告警。
    """
    circuit_heads = [
        path.head_m or 0.0
        for path in paths
        if path.system == "chilled_water" and path.closed_circuit and (path.head_m or 0.0) > 0
    ]
    if circuit_heads:
        return round(max(circuit_heads), 3)
    return round(
        sum(
            item.pump_head_m or 0.0
            for item in calculations
            if item.system == "chilled_water"
        ),
        3,
    )


def _issues_from_circuits(request: SystemEvaluateRequest, paths: list[NetworkPathCalculation]) -> list[Issue]:
    """环路不闭合时告警

    教学软件里画到一半是常态，但扬程只能按兜底估，必须让学生知道
    这个数不是按最不利环路算出来的。
    """
    issues: list[Issue] = []
    for system in WATER_SYSTEMS:
        if not any(pipe.system == system for pipe in request.pipes):
            continue
        if any(path.system == system and path.closed_circuit for path in paths):
            continue
        issues.append(
            Issue(
                level="warning",
                code="WATER_CIRCUIT_NOT_CLOSED",
                message=f"{system} has no closed supply-return circuit; pump head falls back to a conservative sum.",
                target=system,
            )
        )
    return issues


def _balancing_summary(paths: list[NetworkPathCalculation]) -> dict[str, dict[str, float | int | None]]:
    summary: dict[str, dict[str, float | int | None]] = {}
    for system in WATER_SYSTEMS:
        drops = [path.pressure_drop_pa for path in paths if path.system == system and path.pressure_drop_pa > 0]
        if not drops:
            continue
        max_drop = max(drops)
        min_drop = min(drops)
        deviation = (max_drop - min_drop) / max_drop if max_drop > 0 and len(drops) > 1 else 0.0
        summary[system] = {
            "path_count": len(drops),
            "min_pressure_drop_pa": round(min_drop, 3),
            "max_pressure_drop_pa": round(max_drop, 3),
            "imbalance_ratio": round(deviation, 3),
        }
    return summary


def _issues_from_balancing(balancing: dict[str, dict[str, float | int | None]]) -> list[Issue]:
    issues: list[Issue] = []
    for system, item in balancing.items():
        if float(item.get("imbalance_ratio") or 0.0) > 0.15:
            issues.append(Issue(level="warning", code="HYDRAULIC_IMBALANCE", message=f"{system} parallel path pressure deviation is above 15%.", target=system))
    return issues


def _retarget_issues(issues: list[Issue], target: str) -> list[Issue]:
    return [Issue(level=issue.level, code=issue.code, message=issue.message, target=target) for issue in issues]


def _dedupe_issues(issues: list[Issue]) -> list[Issue]:
    seen = set()
    deduped = []
    for issue in issues:
        key = (issue.level, issue.code, issue.message, issue.target)
        if key not in seen:
            seen.add(key)
            deduped.append(issue)
    return deduped
