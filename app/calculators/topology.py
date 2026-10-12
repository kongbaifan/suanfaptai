from collections import defaultdict, deque

from app.building import (
    floor_base_summary,
    floor_weight_map,
    implicit_floor_id,
    is_riser,
    node_floor_id,
    pipe_floor_id,
    pipe_floor_span,
    served_floor_ids,
)
from app.data.equipment import flow_for_device, role_for_device
from app.schemas import (
    Issue,
    SystemEvaluateRequest,
    TopologyAnalysisResult,
    TopologyPath,
)
from app.unity_catalog import DeviceRole, get_device_role
from app.validators.topology import validate_topology

WATER_SYSTEMS = {"chilled_water", "cooling_water", "hot_water"}
ALL_SYSTEMS = ("chilled_water", "cooling_water", "hot_water", "air")
# 按需求路由的边界：(冷热源角色, 末端角色)。冷却水的"末端"是冷却塔；热泵可逆，
# 三种水系统都可能作源。
WATER_ROUTING_ROLES: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "chilled_water": (frozenset({"cold_source", "heat_source"}), frozenset({"terminal"})),
    "hot_water": (frozenset({"cold_source", "heat_source"}), frozenset({"terminal"})),
    "cooling_water": (frozenset({"cold_source", "heat_source"}), frozenset({"cooling_tower"})),
}


def analyze_topology(
    request: SystemEvaluateRequest,
    flow_by_system_m3_h: dict[str, float] | None = None,
) -> TopologyAnalysisResult:
    flow_by_system_m3_h = flow_by_system_m3_h or {}
    devices = {device.id: device for device in request.devices}
    pipes = {pipe.id: pipe for pipe in request.pipes}
    known_ids = set(devices) | set(pipes)
    adjacency = _build_adjacency(request, known_ids)

    components_by_system = {system: _components_for_system(system, adjacency) for system in ALL_SYSTEMS}
    edge_count_by_system = _edge_count_by_system(request)
    open_nodes_by_system = {
        system: _open_nodes_for_system(system, adjacency)
        for system in ALL_SYSTEMS
        if edge_count_by_system.get(system, 0) > 0
    }
    loop_count_by_system = {
        system: _loop_count(edge_count_by_system.get(system, 0), components_by_system.get(system, []))
        for system in ALL_SYSTEMS
        if edge_count_by_system.get(system, 0) > 0
    }
    isolated_nodes = sorted(node_id for node_id in known_ids if node_id not in adjacency)
    paths = _critical_paths(request, devices, adjacency)
    unrouted_systems: list[str] = []
    pipe_flow_m3_h = _pipe_flow_allocation(request, components_by_system, flow_by_system_m3_h, unrouted_systems)
    issues = list(validate_topology(request)) if request.connections else []
    issues.extend(_direction_issues(request, devices, adjacency))
    for system in unrouted_systems:
        issues.append(
            Issue(
                level="warning",
                code="PIPE_FLOW_ROUTING_FALLBACK",
                message=(
                    f"{system} 接线中有不经过冷热源或末端的环路，无法按末端需求分配管段流量，"
                    "已退回按管段类型估算；汇合点进出流量可能不相等。请检查是否有管段首尾相接成环、"
                    "或冷热源 / 末端的型号与类型是否能被设备目录识别。"
                ),
                target=system,
            )
        )
    vertical_network = _vertical_network(request)

    return TopologyAnalysisResult(
        node_count=len(known_ids),
        edge_count_by_system=dict(edge_count_by_system),
        components_by_system={system: [sorted(component) for component in components] for system, components in components_by_system.items() if components},
        open_nodes_by_system=open_nodes_by_system,
        loop_count_by_system=loop_count_by_system,
        isolated_nodes=isolated_nodes,
        paths=paths,
        pipe_flow_m3_h=pipe_flow_m3_h,
        floor_summary=floor_base_summary(request),
        vertical_network=vertical_network,
        issues=_dedupe_issues(issues),
    )


def _build_adjacency(request: SystemEvaluateRequest, known_ids: set[str]) -> dict[str, list[tuple[str, str]]]:
    adjacency: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for connection in request.connections:
        if connection.from_id not in known_ids or connection.to_id not in known_ids:
            continue
        adjacency[connection.from_id].append((connection.to_id, connection.system))
        adjacency[connection.to_id].append((connection.from_id, connection.system))
    return adjacency


def _edge_count_by_system(request: SystemEvaluateRequest) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for connection in request.connections:
        counts[connection.system] += 1
    return counts


def _components_for_system(system: str, adjacency: dict[str, list[tuple[str, str]]]) -> list[set[str]]:
    system_nodes = {node for node, edges in adjacency.items() if any(edge_system == system for _, edge_system in edges)}
    visited: set[str] = set()
    components: list[set[str]] = []

    for start in sorted(system_nodes):
        if start in visited:
            continue
        component: set[str] = set()
        queue = deque([start])
        visited.add(start)
        while queue:
            node = queue.popleft()
            component.add(node)
            for neighbor, edge_system in adjacency.get(node, []):
                if edge_system == system and neighbor not in visited:
                    visited.add(neighbor)
                    queue.append(neighbor)
        components.append(component)
    return components


def _open_nodes_for_system(system: str, adjacency: dict[str, list[tuple[str, str]]]) -> list[str]:
    open_nodes = []
    for node, edges in adjacency.items():
        degree = sum(1 for _, edge_system in edges if edge_system == system)
        if degree == 1:
            open_nodes.append(node)
    return sorted(open_nodes)


def _loop_count(edge_count: int, components: list[set[str]]) -> int:
    node_count = sum(len(component) for component in components)
    return max(edge_count - node_count + len(components), 0)


def _critical_paths(request: SystemEvaluateRequest, devices: dict, adjacency: dict[str, list[tuple[str, str]]]) -> list[TopologyPath]:
    roles = {device_id: role_for_device(device) for device_id, device in devices.items()}
    cold_sources = [node for node, role in roles.items() if role == "cold_source"]
    pumps = [node for node, role in roles.items() if role == "pump"]
    terminals = [node for node, role in roles.items() if role == "terminal"]
    towers = [node for node, role in roles.items() if role == "cooling_tower"]
    paths: list[TopologyPath] = []

    chilled_nodes = _path_through("chilled_water", cold_sources, pumps, terminals, adjacency)
    paths.append(TopologyPath(system="chilled_water", name="cold_source_pump_terminal", nodes=chilled_nodes, floor_ids=_path_floor_ids(request, chilled_nodes), complete=bool(chilled_nodes)))

    cooling_nodes = _shortest_path("cooling_water", cold_sources, towers, adjacency)
    paths.append(TopologyPath(system="cooling_water", name="cold_source_cooling_tower", nodes=cooling_nodes, floor_ids=_path_floor_ids(request, cooling_nodes), complete=bool(cooling_nodes)))

    air_nodes = _shortest_path("air", terminals, [], adjacency)
    if air_nodes:
        paths.append(TopologyPath(system="air", name="terminal_air_path", nodes=air_nodes, floor_ids=_path_floor_ids(request, air_nodes), complete=True))
    return paths


def _path_through(system: str, starts: list[str], middle: list[str], goals: list[str], adjacency) -> list[str]:
    if not starts or not goals:
        return []
    if not middle:
        return _shortest_path(system, starts, goals, adjacency)
    first = _shortest_path(system, starts, middle, adjacency)
    if not first:
        return []
    second = _shortest_path(system, [first[-1]], goals, adjacency)
    if not second:
        return []
    return first + second[1:]


def _shortest_path(system: str, starts: list[str], goals: list[str], adjacency) -> list[str]:
    if not starts:
        return []
    if not goals:
        for start in starts:
            for neighbor, edge_system in adjacency.get(start, []):
                if edge_system == system:
                    return [start, neighbor]
        return []
    goal_set = set(goals)
    queue = deque((start, [start]) for start in starts)
    visited = set(starts)
    while queue:
        node, path = queue.popleft()
        if node in goal_set and len(path) > 1:
            return path
        for neighbor, edge_system in adjacency.get(node, []):
            if edge_system != system or neighbor in visited:
                continue
            visited.add(neighbor)
            queue.append((neighbor, path + [neighbor]))
    return []


def _pipe_flow_allocation(
    request: SystemEvaluateRequest,
    components_by_system: dict[str, list[set[str]]],
    flow_by_system_m3_h: dict[str, float],
    unrouted_systems: list[str] | None = None,
) -> dict[str, float]:
    """管段设计流量

    顺序：管段上显式写的流量 → 沿接线按末端需求路由（水系统）→ 按管段类型估算
    → 按连通分量均分。路由只在接线能从末端追到冷热源时生效，且只填路由出正
    流量的管段；旁通、悬空管段等路由为 0 的仍按原规则估算。

    接线里出现脱离冷热源的环路、无法路由时，该系统整体退回按类型估算，系统名
    追加到 `unrouted_systems`，由调用方报降级告警。
    """
    flows: dict[str, float] = {}
    pipes_by_id = {pipe.id: pipe for pipe in request.pipes}
    weights = floor_weight_map(request)
    for system in ALL_SYSTEMS:
        system_pipes = [pipe for pipe in request.pipes if pipe.system == system]
        explicit = {pipe.id: float(pipe.flow_m3_h) for pipe in system_pipes if pipe.flow_m3_h}
        for pipe_id, flow in explicit.items():
            flows[pipe_id] = round(flow, 3)

        remaining = [pipe for pipe in system_pipes if pipe.id not in explicit]
        if not remaining:
            continue
        total_flow = float(flow_by_system_m3_h.get(system, 0.0))
        if total_flow <= 0:
            total_flow = sum(explicit.values())
        if total_flow <= 0:
            total_flow = float(len(system_pipes))

        if system in WATER_SYSTEMS:
            try:
                # 显式流量只覆盖它自己那根管，其余管段仍按系统总流量路由。
                routed = _routed_pipe_flows(request, system, total_flow)
            except _RoutingCycle:
                routed = None
                if unrouted_systems is not None:
                    unrouted_systems.append(system)
            if routed:
                routed_remaining = []
                for pipe in remaining:
                    flow = routed.get(pipe.id, 0.0)
                    if flow > 0:
                        flows[pipe.id] = round(flow, 3)
                    else:
                        routed_remaining.append(pipe)
                remaining = routed_remaining

        typed_remaining = []
        for pipe in remaining:
            typed_flow = _typed_pipe_flow(request, pipe, total_flow, weights, system_pipes)
            if typed_flow is None:
                typed_remaining.append(pipe)
            else:
                flows[pipe.id] = round(typed_flow, 3)
        remaining = typed_remaining

        for component in components_by_system.get(system, []):
            component_pipes = [pipe for pipe in remaining if pipe.id in component]
            if not component_pipes:
                continue
            component_explicit = sum(flow for pipe_id, flow in explicit.items() if pipe_id in component)
            component_flow = max(total_flow - component_explicit, 0.0) or total_flow
            for pipe in component_pipes:
                flows[pipe.id] = round(component_flow / max(len(component_pipes), 1), 3)

        for pipe in remaining:
            flows.setdefault(pipe.id, round(total_flow / max(len(remaining), 1), 3))

    return {pipe_id: flow for pipe_id, flow in flows.items() if pipe_id in pipes_by_id}


class _RoutingCycle(Exception):
    """接线里有不经过冷热源 / 末端的环路，按需求路由无从定序。"""


def _water_role(device) -> str | None:
    """水力路由用的角色：目录角色优先，型号留空时退到权威类型角色表。

    型号留空时 `role_for_device` 返回 None（这些类型没有 `*` 回退行），整条环路
    就会被误判成脱离冷热源的环路。类型角色表是同一份权威口径，见 CLAUDE.md
    「型号留空：两条降级链不一致」。
    """
    role = role_for_device(device)
    if role:
        return role
    device_role = get_device_role(device.type)
    if device_role is None:
        return None
    return {
        DeviceRole.CHILLER: "cold_source",
        DeviceRole.HEAT_SOURCE: "heat_source",
        DeviceRole.COOLING_TOWER: "cooling_tower",
        DeviceRole.FCU: "terminal",
        DeviceRole.AHU: "terminal",
        DeviceRole.FRESH_AIR_UNIT: "terminal",
    }.get(device_role)


def _terminal_design_flows(request: SystemEvaluateRequest, system: str, terminals: list, total_flow: float) -> dict[str, float]:
    """把系统总流量摊到各末端（m³/h），Σ = total_flow。

    口径与按楼层面积估算一致：每个房间分得 `总流量 × 房间面积 / Σ面积`，再按
    服务它的各末端额定流量（`flow_for_device`，已乘台数）分给这些末端。只有
    房间认领了末端才有面积权重；一个房间都没认领时（冷却塔、无绑定的旧项目）
    退到按额定流量比例，额定也取不到时均分。
    """
    terminal_ids = {device.id for device in terminals}
    rated = {device.id: flow_for_device(device, system) for device in terminals}
    weight = dict.fromkeys(terminal_ids, 0.0)
    for room in request.rooms:
        served = [device_id for device_id in dict.fromkeys(room.device_ids) if device_id in terminal_ids]
        if not served:
            continue
        rated_sum = sum(rated[device_id] for device_id in served)
        for device_id in served:
            share = rated[device_id] / rated_sum if rated_sum > 0 else 1.0 / len(served)
            weight[device_id] += room.area_m2 * share
    if sum(weight.values()) <= 0:
        weight = dict(rated)
    if sum(weight.values()) <= 0:
        weight = dict.fromkeys(terminal_ids, 1.0)
    total_weight = sum(weight.values())
    return {device_id: total_flow * value / total_weight for device_id, value in weight.items()}


def _routed_pipe_flows(request: SystemEvaluateRequest, system: str, total_flow: float) -> dict[str, float] | None:
    """沿接线把末端设计流量汇到管段（m³/h），汇合点进出按构造相等。

    供水侧：管段流量 = 下游末端需求之和；回水侧镜像，取上游末端需求之和。
    冷热源与末端是边界，遍历到此为止；泵、阀、分集水器直接穿过。一个节点有
    多根供水管段汇入时，它的需求在这些管段间均分（并联取同等份额）。

    拿不到末端或接线里没有管段时返回 None，由调用方按管段类型估算。
    """
    sources, sinks = WATER_ROUTING_ROLES[system]
    devices = {device.id: device for device in request.devices}
    pipe_ids = {pipe.id for pipe in request.pipes if pipe.system == system}
    successors: defaultdict[str, list[str]] = defaultdict(list)
    predecessors: defaultdict[str, list[str]] = defaultdict(list)
    for connection in request.connections:
        if connection.system != system:
            continue
        ends = (connection.from_id, connection.to_id)
        # 设备↔设备的连接（冷机→泵）照样参与遍历；悬空引用不参与。
        if not all(node in devices or node in pipe_ids for node in ends):
            continue
        successors[connection.from_id].append(connection.to_id)
        predecessors[connection.to_id].append(connection.from_id)

    linked = set(successors) | set(predecessors)
    if not linked & pipe_ids:
        return None
    roles = {device_id: _water_role(devices[device_id]) for device_id in linked if device_id in devices}
    terminals = [devices[device_id] for device_id, role in roles.items() if role in sinks]
    if not terminals:
        return None
    demand = _terminal_design_flows(request, system, terminals, total_flow)
    boundary = {device_id for device_id, role in roles.items() if role in sources or role in sinks}

    def accumulate(downstream: defaultdict[str, list[str]], upstream: defaultdict[str, list[str]]) -> dict[str, float]:
        memo: dict[str, float] = {}

        def visit(node: str, stack: set[str]) -> float:
            if node in memo:
                return memo[node]
            if node in stack:
                raise _RoutingCycle(node)
            stack.add(node)
            total = 0.0
            for child in dict.fromkeys(downstream[node]):
                if child in demand:
                    child_need = demand[child]
                elif child in boundary:
                    continue  # 走回冷热源：供水侧到此为止
                else:
                    child_need = visit(child, stack)
                feeders = sum(1 for parent in dict.fromkeys(upstream[child]) if parent not in boundary)
                total += child_need / max(feeders, 1)
            stack.discard(node)
            memo[node] = total
            return total

        return {pipe_id: visit(pipe_id, set()) for pipe_id in pipe_ids}

    supply = accumulate(successors, predecessors)
    collected = accumulate(predecessors, successors)
    return {pipe_id: max(supply[pipe_id], collected[pipe_id]) for pipe_id in pipe_ids}


def _typed_pipe_flow(request: SystemEvaluateRequest, pipe, total_flow: float, weights: dict[str, float], system_pipes: list) -> float | None:
    if pipe.pipe_type == "unknown":
        return None
    if pipe.pipe_type in {"main_supply", "main_return"}:
        return total_flow
    if pipe.pipe_type in {"riser_supply", "riser_return"}:
        served_weight = sum(weights.get(floor_id, 0.0) for floor_id in served_floor_ids(request, pipe))
        return total_flow * (served_weight or 1.0)
    if pipe.pipe_type in {"branch_supply", "branch_return"}:
        return total_flow * weights.get(
            pipe_floor_id(pipe, implicit=implicit_floor_id(request)), 0.0
        )
    if pipe.pipe_type == "terminal_branch":
        implicit = implicit_floor_id(request)
        floor_id = pipe_floor_id(pipe, implicit=implicit)
        same_floor = [
            item
            for item in system_pipes
            if item.pipe_type == "terminal_branch"
            and pipe_floor_id(item, implicit=implicit) == floor_id
        ]
        floor_flow = total_flow * weights.get(floor_id, 0.0)
        return floor_flow / max(len(same_floor), 1)
    if pipe.pipe_type == "bypass":
        return total_flow * 0.1
    return None


def _direction_issues(request: SystemEvaluateRequest, devices: dict, adjacency) -> list[Issue]:
    issues: list[Issue] = []
    for device_id, device in devices.items():
        role = role_for_device(device)
        systems = {system for _, system in adjacency.get(device_id, [])}
        if role == "cooling_tower" and "cooling_water" not in systems:
            issues.append(Issue(level="warning", code="COOLING_TOWER_NOT_IN_COOLING_WATER", message=f"Cooling tower {device_id} is not connected to cooling-water topology.", target=device_id))
        if role == "cold_source" and not (systems & WATER_SYSTEMS):
            issues.append(Issue(level="warning", code="COLD_SOURCE_NOT_IN_WATER_LOOP", message=f"Cold source {device_id} is not connected to a water loop.", target=device_id))
    return issues


def _vertical_network(request: SystemEvaluateRequest) -> dict:
    risers = []
    invalid_pipes = []
    for pipe in request.pipes:
        start_floor, end_floor = pipe_floor_span(
            pipe, implicit=implicit_floor_id(request)
        )
        if is_riser(pipe):
            risers.append(
                {
                    "pipe_id": pipe.id,
                    "system": pipe.system,
                    "pipe_type": pipe.pipe_type,
                    "from_floor_id": start_floor,
                    "to_floor_id": end_floor,
                    "served_floor_ids": served_floor_ids(request, pipe),
                }
            )
        elif start_floor != end_floor:
            invalid_pipes.append(
                {
                    "pipe_id": pipe.id,
                    "system": pipe.system,
                    "pipe_type": pipe.pipe_type,
                    "from_floor_id": start_floor,
                    "to_floor_id": end_floor,
                }
            )

    cross_floor_connections = []
    invalid_connections = []
    pipes_by_id = {pipe.id: pipe for pipe in request.pipes}
    for connection in request.connections:
        from_floor = node_floor_id(request, connection.from_id)
        to_floor = node_floor_id(request, connection.to_id)
        if from_floor == to_floor:
            continue
        item = {
            "from_id": connection.from_id,
            "to_id": connection.to_id,
            "system": connection.system,
            "from_floor_id": from_floor,
            "to_floor_id": to_floor,
        }
        cross_floor_connections.append(item)
        from_pipe = pipes_by_id.get(connection.from_id)
        to_pipe = pipes_by_id.get(connection.to_id)
        if not ((from_pipe and is_riser(from_pipe)) or (to_pipe and is_riser(to_pipe))):
            invalid_connections.append(item)

    return {
        "risers": risers,
        "cross_floor_connections": cross_floor_connections,
        "invalid_cross_floor_connections": invalid_connections,
        "invalid_cross_floor_pipes": invalid_pipes,
    }


def _path_floor_ids(request: SystemEvaluateRequest, nodes: list[str]) -> list[str]:
    """路径所经节点归属楼层的去重列表，保持首次出现顺序

    与 `air_network._path_floor_ids`、`hydraulic_network._path_floor_ids`
    保持同一实现。此处原有一个恒真条件：

        if floor_id != DEFAULT_FLOOR_ID or floor_id not in floor_ids:
            if floor_id not in floor_ids:

    记 X = (floor_id not in floor_ids)，整体为 `X and (A or X) ≡ X`，
    即 `floor_id != DEFAULT_FLOOR_ID` 从不影响结果——`DEFAULT_FLOOR_ID` 是
    设备未声明楼层时的兜底归属（见 building.py 的 device_floor_id），不是
    无效楼层，本就不该被过滤掉。已删除该死条件，行为不变。
    """
    floor_ids = []
    for node in nodes:
        floor_id = node_floor_id(request, node)
        if floor_id not in floor_ids:
            floor_ids.append(floor_id)
    return floor_ids


def _dedupe_issues(issues: list[Issue]) -> list[Issue]:
    deduped: list[Issue] = []
    seen = set()
    for issue in issues:
        key = (issue.level, issue.code, issue.message, issue.target)
        if key not in seen:
            seen.add(key)
            deduped.append(issue)
    return deduped
