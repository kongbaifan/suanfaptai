"""Compile project connections once for the bounded teaching runtime.

Water routes require both supply and return and exactly one pump per route.
Parallel pumps/source banks are distinct routes sharing terminal capacities.
Bypasses, ambiguous alternative paths and serial pumps are diagnosed instead
of selecting an arbitrary shortest route. Air roots stop at room outlets;
indoor supply-to-return links do not turn a valid air system into a pipe cycle.
"""

from __future__ import annotations

import math
from collections import defaultdict, deque
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Literal

from app.data.equipment import spec_for_device
from app.data.rules import rule_value
from app.schemas import Issue, PipeInput, SystemEvaluateRequest
from app.simcore.devices import is_pump, is_terminal, is_valve
from app.simcore.network_flow import ElementKey, FlowPath
from app.simcore.rated import RatedSpec
from app.unity_catalog import DeviceRole

NetworkStatus = Literal["modeled", "disconnected", "unsupported"]

WATER_SYSTEMS = ("chilled_water", "cooling_water", "hot_water")
SYSTEM_TO_LOOP = {
    "chilled_water": "chilled",
    "cooling_water": "cooling",
    "hot_water": "hot",
}
WATER_DENSITY = 1000.0
AIR_DENSITY = 1.2
GRAVITY = 9.80665
# Compile-time work guards, not physical tuning parameters.
MAX_ROUTE_COMBINATIONS = 4096
MAX_GRAPH_NODES = 10000


@dataclass(frozen=True, slots=True)
class NetworkElement:
    key: ElementKey
    resistance: float  # Pa / (m³/h)²
    area_m2: float | None = None
    diameter_mm: float | None = None
    pipe: PipeInput | None = None
    valve: bool = False
    parameter_sources: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class NetworkCircuit:
    circuit_id: str
    system: str
    node_ids: tuple[str, ...]
    source_ids: tuple[str, ...]
    drive_ids: tuple[str, ...]
    status: NetworkStatus = "modeled"


@dataclass(frozen=True, slots=True)
class RuntimeNetworkLayout:
    paths: tuple[FlowPath, ...]
    circuits: tuple[NetworkCircuit, ...]
    elements: Mapping[ElementKey, NetworkElement]
    issues: tuple[Issue, ...]
    local_air_terminal_ids: frozenset[str] = frozenset()
    air_target_shares: Mapping[str, float] = field(default_factory=dict)
    fan_pressure_pa: Mapping[str, float] = field(default_factory=dict)
    water_cooled_source_ids: frozenset[str] = frozenset()
    unsupported_keys: frozenset[ElementKey] = frozenset()
    control_room_ids: Mapping[ElementKey, tuple[str, ...]] = field(default_factory=dict)


def _issue(code: str, message: str, target: str | None = None) -> Issue:
    return Issue(level="warning", code=code, message=message, target=target)


def _number(value: object, fallback: float = 0.0) -> float:
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return fallback
    return result if math.isfinite(result) and result >= 0.0 else fallback


def _graph(
    project: SystemEvaluateRequest,
    system: str,
    issues: list[Issue],
    rated: Mapping[str, RatedSpec],
) -> dict[str, tuple[str, ...]]:
    known = {d.id for d in project.devices} | {
        p.id for p in project.pipes if p.system == system
    }
    graph: dict[str, set[str]] = defaultdict(set)
    allowed_media = {
        DeviceRole.CHILLER: {"chilled_water", "cooling_water"},
        DeviceRole.HEAT_SOURCE: {"chilled_water", "hot_water"},
        DeviceRole.COOLING_TOWER: {"cooling_water"},
        DeviceRole.PUMP: set(WATER_SYSTEMS),
        DeviceRole.PUMP_CHILLED_WATER: {"chilled_water"},
        DeviceRole.PUMP_COOLING_WATER: {"cooling_water"},
        DeviceRole.WATER_VALVE: set(WATER_SYSTEMS),
        DeviceRole.AIR_VALVE: {"air"},
        DeviceRole.FIRE_DAMPER: {"air"},
        DeviceRole.AIR_OUTLET: {"air"},
        DeviceRole.FCU: {"air", "chilled_water", "hot_water"},
        DeviceRole.AHU: {"air", "chilled_water", "hot_water"},
        DeviceRole.FRESH_AIR_UNIT: {"air", "chilled_water", "hot_water"},
    }
    invalid = {
        key
        for key, spec in rated.items()
        if system not in allowed_media.get(spec.role, {*WATER_SYSTEMS, "air"})
    }
    for key, spec in rated.items():
        if is_pump(spec.role) or spec.role is DeviceRole.WATER_VALVE:
            media = {
                edge.system
                for edge in project.connections
                if key in {edge.from_id, edge.to_id}
            }
            if len(media) > 1:
                invalid.add(key)
    for pipe in project.pipes:
        if pipe.system == system:
            graph[pipe.id]
    for edge in project.connections:
        if edge.system != system:
            continue
        incompatible = {edge.from_id, edge.to_id} & invalid
        if incompatible:
            for key in sorted(incompatible):
                issues.append(
                    _issue(
                        "SIM_CONNECTION_MEDIUM_MISMATCH",
                        "设备与连接介质不兼容，或单个泵 / 阀门混接了不同介质；请分别建立独立回路。",
                        key,
                    )
                )
            continue
        if edge.from_id not in known or edge.to_id not in known:
            issues.append(
                _issue(
                    "SIM_CONNECTION_INVALID",
                    "连接引用了不存在或介质不符的节点，请检查连接两端。",
                    f"{edge.from_id}->{edge.to_id}",
                )
            )
            continue
        graph[edge.from_id].add(edge.to_id)
        graph[edge.to_id]
    return {key: tuple(sorted(value)) for key, value in sorted(graph.items())}


def _reverse(graph: Mapping[str, tuple[str, ...]]) -> dict[str, tuple[str, ...]]:
    reversed_graph: dict[str, list[str]] = {key: [] for key in graph}
    for key, neighbors in graph.items():
        for neighbor in neighbors:
            reversed_graph.setdefault(neighbor, []).append(key)
    return {key: tuple(sorted(value)) for key, value in reversed_graph.items()}


def _reachable(
    start: str, graph: Mapping[str, tuple[str, ...]], blocked: set[str]
) -> set[str]:
    seen = {start}
    pending = deque([start])
    while pending:
        node = pending.popleft()
        for neighbor in graph.get(node, ()):
            if neighbor in blocked or neighbor in seen:
                continue
            seen.add(neighbor)
            pending.append(neighbor)
    return seen


def unique_route(
    start: str, end: str, graph: Mapping[str, tuple[str, ...]], blocked: set[str]
) -> tuple[tuple[str, ...], bool]:
    """Return the only directed route and whether alternatives/cycles exist.

    Reachability plus a topological path count capped at two is O(V+E).
    No enumeration of all simple paths, including in malformed student graphs.
    """
    blocked = blocked - {start, end}
    forward = _reachable(start, graph, blocked | {end})
    # End is an absorbing boundary; include it only if actually reached.
    if end != start and not any(end in graph.get(node, ()) for node in forward):
        return (), False
    forward.add(end)
    backward = _reachable(end, _reverse(graph), blocked | {start}) | {start}
    relevant = forward & backward
    incoming = {key: 0 for key in relevant}
    for key in relevant - {end}:
        for neighbor in graph.get(key, ()):
            if neighbor in relevant:
                incoming[neighbor] += 1
    queue = deque(sorted(key for key, count in incoming.items() if count == 0))
    counts = {key: 0 for key in relevant}
    counts[start] = 1
    parent: dict[str, str] = {}
    visited = 0
    while queue:
        key = queue.popleft()
        visited += 1
        if key == end:
            continue
        for neighbor in graph.get(key, ()):
            if neighbor not in relevant:
                continue
            counts[neighbor] = min(2, counts[neighbor] + counts[key])
            if counts[key]:
                parent[neighbor] = key
            incoming[neighbor] -= 1
            if incoming[neighbor] == 0:
                queue.append(neighbor)
    if visited != len(relevant) or counts.get(end, 0) > 1:
        return (), True
    if counts.get(end, 0) != 1:
        return (), False
    route = [end]
    while route[-1] != start:
        route.append(parent[route[-1]])
    return tuple(reversed(route)), False


def _components(graph: Mapping[str, tuple[str, ...]]) -> list[tuple[str, ...]]:
    reverse = _reverse(graph)
    unseen = set(graph)
    result = []
    while unseen:
        start = min(unseen)
        component = {start}
        pending = [start]
        while pending:
            node = pending.pop()
            for other in (*graph.get(node, ()), *reverse.get(node, ())):
                if other not in component:
                    component.add(other)
                    pending.append(other)
        unseen.difference_update(component)
        result.append(tuple(sorted(component)))
    return result


def _water_paths(
    project: SystemEvaluateRequest,
    rated: Mapping[str, RatedSpec],
    system: str,
    graph: Mapping[str, tuple[str, ...]],
    issues: list[Issue],
) -> tuple[list[FlowPath], list[NetworkCircuit], set[ElementKey]]:
    paths: list[FlowPath] = []
    circuits: list[NetworkCircuit] = []
    unsupported: set[ElementKey] = set()
    source_roles = (
        {DeviceRole.CHILLER} if system == "cooling_water" else {DeviceRole.HEAT_SOURCE}
    )
    if system == "chilled_water":
        source_roles.add(DeviceRole.CHILLER)
    all_sources = {key for key, spec in rated.items() if spec.role in source_roles}
    targets = {
        key
        for key, spec in rated.items()
        if (
            spec.role is DeviceRole.COOLING_TOWER
            if system == "cooling_water"
            else is_terminal(spec.role)
        )
    }
    all_pumps = {key for key, spec in rated.items() if is_pump(spec.role)}
    bypasses = {
        p.id for p in project.pipes if p.pipe_type == "bypass" and p.system == system
    }
    multi_valves = {
        d.id for d in project.devices if d.quantity > 1 and is_valve(rated[d.id].role)
    }
    for nodes in _components(graph):
        members = set(nodes)
        sources = tuple(sorted(members & all_sources))
        pumps = tuple(sorted(members & all_pumps))
        ends = tuple(sorted(members & targets))
        circuit_id = f"{system}:{nodes[0]}"
        status: NetworkStatus = "modeled"
        if (
            members & (bypasses | multi_valves)
            or len(sources) * len(pumps) * len(ends) > MAX_ROUTE_COMBINATIONS
        ):
            status = "unsupported"
            unsupported.update((system, node) for node in nodes)
            issues.append(
                _issue(
                    "SIM_NETWORK_UNSUPPORTED",
                    "此子系统包含旁通、位置不明的阀门组或超出有界求解规模，请拆分为明确的供回支路。",
                    circuit_id,
                )
            )
        elif not sources or not pumps:
            status = "disconnected"
            if ends or pumps:
                issues.append(
                    _issue(
                        "SIM_WATER_CIRCUIT_INCOMPLETE",
                        "水回路缺少相连的冷热源（冷却侧为冷机）或水泵。",
                        circuit_id,
                    )
                )
        for target in ends if status == "modeled" else ():
            target_paths = []
            ambiguous = False
            for source in sources:
                for pump in pumps:
                    blocked = (
                        (all_sources - {source})
                        | (targets - {target})
                        | (all_pumps - {pump})
                    )
                    supply, a = unique_route(source, target, graph, blocked)
                    ret, b = unique_route(target, source, graph, blocked)
                    ambiguous |= a or b
                    if not supply or not ret or pump not in (*supply, *ret):
                        continue
                    # Reusing a passive node on both legs implies a cross-link,
                    # not a distinct supply/return circuit.
                    if set(supply[1:-1]) & set(ret[1:-1]):
                        ambiguous = True
                        continue
                    elements = tuple(
                        (system, key) for key in dict.fromkeys((*supply, *ret))
                    )
                    target_paths.append(
                        FlowPath(
                            f"{system}/{source}/{pump}/{target}",
                            system,
                            circuit_id,
                            source,
                            pump,
                            target,
                            elements,
                        )
                    )
            if ambiguous:
                unsupported.add((system, target))
                issues.append(
                    _issue(
                        "SIM_NETWORK_UNSUPPORTED",
                        "该支路存在多条替代路径或交叉连接，当前教学模型不做网状压力平衡。",
                        target,
                    )
                )
            elif target_paths:
                paths.extend(target_paths)
            else:
                issues.append(
                    _issue(
                        "SIM_WATER_PATH_INCOMPLETE",
                        "未找到经过水泵的完整供回通路，请补齐供水、回水并检查方向。",
                        target,
                    )
                )
        circuits.append(
            NetworkCircuit(circuit_id, system, nodes, sources, pumps, status)
        )
    return paths, circuits, unsupported


def _air_paths(
    project: SystemEvaluateRequest,
    rated: Mapping[str, RatedSpec],
    graph: Mapping[str, tuple[str, ...]],
    issues: list[Issue],
    room_weights: Mapping[str, float],
) -> tuple[
    list[FlowPath], list[NetworkCircuit], set[str], dict[str, float], set[ElementKey]
]:
    paths: list[FlowPath] = []
    circuits: list[NetworkCircuit] = []
    local: set[str] = set()
    shares: dict[str, float] = {}
    unsupported: set[ElementKey] = set()
    terminals = {key for key, spec in rated.items() if is_terminal(spec.role)}
    outlets = {key for key, spec in rated.items() if spec.role is DeviceRole.AIR_OUTLET}
    reverse = _reverse(graph)
    devices = {device.id: device for device in project.devices}
    for index, room in enumerate(project.rooms):
        for outlet in (*room.supply_air_outlet_ids, *room.return_air_outlet_ids):
            if outlet not in outlets:
                issues.append(
                    _issue(
                        "SIM_AIR_BINDING_INVALID",
                        "房间的供回风口绑定必须引用风口设备；末端仍填在 device_ids 中。",
                        room.id or f"room_{index + 1}",
                    )
                )
    for root in sorted(terminals):
        spec = rated[root]
        rooms = [
            (r.id or f"room_{i + 1}", r)
            for i, r in enumerate(project.rooms)
            if root in r.device_ids
        ]
        requires_ducts = (
            spec.role is not DeviceRole.FCU
            or root in graph
            or devices[root].air_connection_mode == "ducted"
        )
        if devices[root].air_connection_mode == "room" and root in graph:
            unsupported.add(("air", root))
            issues.append(
                _issue(
                    "SIM_AIR_MODE_CONFLICT",
                    "设备声明为房间直吹却连接了风管；请明确送回风方式。",
                    root,
                )
            )
        if not requires_ducts:
            local.add(root)
            continue
        circuit_id = f"air:{root}"
        reachable_outlets = []
        routes: dict[str, tuple[str, ...]] = {}
        for outlet in sorted(outlets):
            route, ambiguous = unique_route(
                root, outlet, graph, (terminals - {root}) | (outlets - {outlet})
            )
            if ambiguous:
                unsupported.add(("air", root))
                issues.append(
                    _issue(
                        "SIM_AIR_PATH_UNSUPPORTED",
                        "机组到风口存在多条替代风路，请明确送回风方向。",
                        root,
                    )
                )
            if route:
                reachable_outlets.append(outlet)
                routes[outlet] = route
        fresh = spec.role is DeviceRole.FRESH_AIR_UNIT
        intake: tuple[str, ...] = ()
        if fresh:
            upstream = _reachable(root, reverse, (terminals - {root}) | outlets)
            intakes = sorted(key for key in upstream - {root} if not reverse.get(key))
            for entry in intakes:
                candidate, ambiguous = unique_route(
                    entry, root, graph, (terminals - {root}) | outlets
                )
                if candidate and not intake:
                    intake = candidate
                elif candidate or ambiguous:
                    unsupported.add(("air", root))
            if not intake:
                issues.append(
                    _issue(
                        "SIM_FRESH_AIR_INTAKE_MISSING",
                        "新风机组未接通室外取风通路。",
                        root,
                    )
                )
        root_paths: list[FlowPath] = []
        for outlet in reachable_outlets:
            bound = [(key, r) for key, r in rooms if outlet in r.supply_air_outlet_ids]
            if (
                not bound
                and len(reachable_outlets) == 1
                and not any(r.supply_air_outlet_ids for _, r in rooms)
            ):
                bound = rooms
            if not bound:
                issues.append(
                    _issue(
                        "SIM_AIR_OUTLET_BINDING_REQUIRED",
                        "请明确此送风口服务的房间，避免关阀影响到其他房间。",
                        outlet,
                    )
                )
                continue
            return_outlets = {key for _, r in bound for key in r.return_air_outlet_ids}
            if not return_outlets:
                # A legacy single outlet may represent the shared room boundary;
                # a paired return outlet must be directly linked across the room.
                return_outlets = {outlet} | {
                    n for n in graph.get(outlet, ()) if n in outlets
                }
            returns = []
            if not fresh:
                for entry in sorted(return_outlets):
                    route, ambiguous = unique_route(
                        entry, root, graph, (terminals - {root}) | (outlets - {entry})
                    )
                    if route:
                        returns.append(route)
                    if ambiguous:
                        unsupported.add(("air", root))
                if len(returns) != 1:
                    issues.append(
                        _issue(
                            "SIM_AIR_RETURN_INCOMPLETE",
                            "机组送风口未匹配到唯一的房间回风通路。",
                            root,
                        )
                    )
                    continue
            if fresh and not intake:
                continue
            nodes = tuple(
                dict.fromkeys((*routes[outlet], *(intake if fresh else returns[0])))
            )
            target = f"{root}/{outlet}"
            shares[target] = math.fsum(
                max(room_weights.get(key, r.area_m2), 0.0) for key, r in bound
            )
            root_paths.append(
                FlowPath(
                    f"air/{target}",
                    "air",
                    circuit_id,
                    root,
                    root,
                    target,
                    tuple(("air", n) for n in nodes),
                    tuple(key for key, _ in bound),
                )
            )
        if ("air", root) in unsupported:
            root_paths = []
        total = math.fsum(shares[p.target_id] for p in root_paths)
        for path in root_paths:
            shares[path.target_id] = (
                shares[path.target_id] / total if total > 0 else 1.0 / len(root_paths)
            )
        paths.extend(root_paths)
        nodes = tuple(
            sorted({root} | {key[1] for p in root_paths for key in p.elements})
        )
        status: NetworkStatus = (
            "unsupported"
            if ("air", root) in unsupported
            else "modeled"
            if root_paths
            else "disconnected"
        )
        if not root_paths:
            issues.append(
                _issue(
                    "SIM_AIR_PATH_INCOMPLETE",
                    "机组没有可服务房间的完整送回风（或新风）通路。",
                    root,
                )
            )
        circuits.append(
            NetworkCircuit(circuit_id, "air", nodes, (root,), (root,), status)
        )
    return paths, circuits, local, shares, unsupported


def _elements(
    project: SystemEvaluateRequest,
    rated: Mapping[str, RatedSpec],
    graphs: Mapping[str, Mapping[str, tuple[str, ...]]],
) -> dict[ElementKey, NetworkElement]:
    elements: dict[ElementKey, NetworkElement] = {}
    pipes = {p.id: p for p in project.pipes}
    devices = {d.id: d for d in project.devices}
    default_d = float(rule_value("runtime_network.default_water_diameter_mm", 100.0))
    for system, graph in graphs.items():
        air = system == "air"
        density = AIR_DENSITY if air else WATER_DENSITY
        friction = float(
            rule_value(
                "pressure_defaults.air_friction_factor"
                if air
                else "pressure_defaults.water_friction_factor",
                0.02 if air else 0.025,
            )
        )
        for key in graph:
            pipe = pipes.get(key)
            if pipe is not None:
                sources = ["input:geometry", "rules:pressure_defaults"]
                if air and pipe.width_mm and pipe.height_mm:
                    a, b = pipe.width_mm / 1000.0, pipe.height_mm / 1000.0
                    area, diameter = a * b, 2.0 * a * b / (a + b)
                else:
                    diameter_mm = pipe.diameter_mm or default_d
                    diameter = diameter_mm / 1000.0
                    area = math.pi * diameter**2 / 4.0
                    if pipe.diameter_mm is None:
                        sources[0] = "rules:runtime_network.default_water_diameter_mm"
                resistance = (
                    (
                        friction * pipe.length_m / diameter
                        + pipe.local_resistance_coefficient
                    )
                    * density
                    / (2.0 * (3600.0 * area) ** 2)
                )
                elements[(system, key)] = NetworkElement(
                    (system, key),
                    resistance,
                    area,
                    diameter * 1000.0,
                    pipe,
                    False,
                    tuple(sources),
                )
                continue
            device = devices[key]
            spec = rated[key]
            catalog = spec_for_device(device) or {}
            valve = is_valve(spec.role) and spec.device_type != "止回阀"
            flow = (
                spec.airflow_m3_h
                if air
                else spec.cooling_flow_m3_h
                if system == "cooling_water" and spec.role is DeviceRole.CHILLER
                else spec.flow_m3_h
            )
            reference_flow = (flow or 0.0) * device.quantity
            field_name = (
                "air_pressure_drop_pa"
                if air
                else "cooling_pressure_drop_kpa"
                if system == "cooling_water"
                else "chilled_pressure_drop_kpa"
                if spec.role is DeviceRole.CHILLER
                else "water_pressure_drop_kpa"
            )
            drop = _number(catalog.get(field_name)) * (1.0 if air else 1000.0)
            origin = f"catalog:{field_name}"
            if drop <= 0:
                drop = device.pressure_drop_pa or _number(
                    catalog.get("pressure_drop_pa")
                )
                origin = (
                    "input:pressure_drop_pa"
                    if device.pressure_drop_pa is not None
                    else "catalog:pressure_drop_pa"
                )
            resistance = drop / reference_flow**2 if reference_flow > 0 else 0.0
            sources = [origin] if resistance > 0 else []
            if valve or (spec.role is DeviceRole.AIR_OUTLET):
                peers = set(graph.get(key, ())) | {
                    a for a, b in graph.items() if key in b
                }
                diameter_mm = min(
                    (
                        float(pipes[p].diameter_mm or default_d)
                        for p in peers
                        if p in pipes and pipes[p].diameter_mm
                    ),
                    default=default_d,
                )
                area = math.pi * (diameter_mm / 1000.0) ** 2 / 4.0
                if air:
                    areas = [
                        (pipes[p].width_mm or 0.0) * (pipes[p].height_mm or 0.0) / 1e6
                        for p in peers
                        if p in pipes and pipes[p].width_mm and pipes[p].height_mm
                    ]
                    if areas:
                        area = min(areas)
                # An outlet's recommended airflow and nominal design drop are
                # not a measured pressure/flow curve. Use its declared zeta
                # with the connected duct area instead of inventing R=30/500².
                if resistance <= 0 or air:
                    zeta = _number(
                        catalog.get("local_resistance_coefficient"),
                        float(
                            rule_value(
                                "pressure_defaults.air_local_k"
                                if air
                                else "pressure_defaults.water_local_k",
                                1.5 if air else 2.0,
                            )
                        ),
                    )
                    resistance = zeta * density / (2.0 * (3600.0 * area) ** 2)
                    sources = [
                        "catalog:local_resistance_coefficient"
                        if "local_resistance_coefficient" in catalog
                        else "rules:pressure_defaults",
                        "calculated:adjacent_pipe_area",
                    ]
            elements[(system, key)] = NetworkElement(
                (system, key), resistance, valve=valve, parameter_sources=tuple(sources)
            )
    return elements


def compile_runtime_network(
    project: SystemEvaluateRequest,
    rated: Mapping[str, RatedSpec],
    room_weights: Mapping[str, float],
) -> RuntimeNetworkLayout:
    issues: list[Issue] = []
    graphs = {
        system: _graph(project, system, issues, rated)
        for system in (*WATER_SYSTEMS, "air")
    }
    if len(project.devices) + len(project.pipes) > MAX_GRAPH_NODES:
        issues.append(
            _issue(
                "SIM_NETWORK_SIZE_UNSUPPORTED",
                "网络节点数超出教学求解上限，请拆分项目。",
            )
        )
        return RuntimeNetworkLayout((), (), {}, tuple(issues))
    paths: list[FlowPath] = []
    circuits: list[NetworkCircuit] = []
    unsupported: set[ElementKey] = set()
    for system in WATER_SYSTEMS:
        water_paths, water_circuits, limited = _water_paths(
            project, rated, system, graphs[system], issues
        )
        paths.extend(water_paths)
        circuits.extend(water_circuits)
        unsupported.update(limited)
    air_paths, air_circuits, local, shares, limited = _air_paths(
        project, rated, graphs["air"], issues, room_weights
    )
    paths.extend(air_paths)
    circuits.extend(air_circuits)
    unsupported.update(limited)
    fan_pressure = {}
    water_cooled = set()
    for device in project.devices:
        spec = rated[device.id]
        catalog = spec_for_device(device) or {}
        if spec.role is DeviceRole.CHILLER and (catalog.get("water_cooled", True)):
            water_cooled.add(device.id)
        if is_terminal(spec.role):
            fan_pressure[device.id] = (
                _number(project.design_options.get("fan_static_pressure_pa"))
                or _number(catalog.get("external_static_pressure_pa"))
                or float(rule_value("runtime_network.fan_static_pressure_pa", 500.0))
            )
    elements = _elements(project, rated, graphs)
    for device in project.devices:
        key = ("air", device.id)
        if key in elements and device.id in fan_pressure:
            catalog = spec_for_device(device) or {}
            origin = (
                "input:fan_static_pressure_pa"
                if _number(project.design_options.get("fan_static_pressure_pa"))
                else "catalog:external_static_pressure_pa"
                if _number(catalog.get("external_static_pressure_pa"))
                else "rules:runtime_network.fan_static_pressure_pa"
            )
            elements[key] = replace(
                elements[key],
                parameter_sources=(*elements[key].parameter_sources, origin),
            )
    control_rooms: dict[ElementKey, set[str]] = defaultdict(set)
    rooms_by_terminal: dict[str, set[str]] = defaultdict(set)
    for index, room in enumerate(project.rooms):
        for terminal_id in room.device_ids:
            rooms_by_terminal[terminal_id].add(room.id or f"room_{index + 1}")
    for path in paths:
        if path.system not in {"chilled_water", "hot_water"}:
            continue
        for device_id in (path.source_id, path.drive_id, path.target_id):
            control_rooms[(path.system, device_id)].update(
                rooms_by_terminal[path.target_id]
            )
    for path in paths:
        if path.system == "cooling_water":
            for device_id in (path.source_id, path.drive_id, path.target_id):
                control_rooms[("cooling_water", device_id)].update(
                    control_rooms[("chilled_water", path.source_id)]
                )
    issues = sorted(
        {
            (issue.level, issue.code, issue.target, issue.message): issue
            for issue in issues
        }.values(),
        key=lambda issue: (issue.code, issue.target or "", issue.message),
    )
    return RuntimeNetworkLayout(
        tuple(sorted(paths, key=lambda p: p.path_id)),
        tuple(circuits),
        elements,
        tuple(issues),
        frozenset(local),
        shares,
        fan_pressure,
        frozenset(water_cooled),
        frozenset(unsupported),
        {key: tuple(sorted(value)) for key, value in control_rooms.items()},
    )
