"""管网路径枚举

水系统按**完整环路**枚举：冷源 → 供水管 → 末端 → 回水管 → 回到冷源。
只枚举单管程（走到末端就停）会漏掉回程阻力，水泵扬程按此选型偏小近一半。

风系统仍按单向枚举：送风管路是开式的，室内送风口就是终点，
空气不经管道回到风机，不存在闭合环。
"""

from collections import defaultdict

from app.data.equipment import role_for_device
from app.schemas import SystemEvaluateRequest


WATER_SYSTEMS = ("chilled_water", "cooling_water", "hot_water")


def enumerate_network_paths(request: SystemEvaluateRequest, system: str, max_paths: int = 24) -> list[list[str]]:
    graph = _graph_for_system(request, system)
    if not graph:
        return []

    starts, goals = _path_endpoints(request, system, graph)
    if not starts:
        return []

    if system in WATER_SYSTEMS:
        circuits = _water_circuits(graph, starts, set(goals), max_paths)
        if circuits:
            return circuits
        # 环路不闭合（图画到一半、缺回水管）时退回单管程枚举，
        # 让学生仍能看到已画出的部分；扬程侧用 is_closed_circuit 区分，
        # 不会把半条路径当成完整环路去选泵。

    if not goals:
        goals = _open_nodes(graph) or [node for node in graph if node not in starts]

    paths: list[list[str]] = []
    for start in starts:
        _dfs(graph, start, set(goals), [start], paths, max_paths)
        if len(paths) >= max_paths:
            break
    return _dedupe_paths(paths)


def is_closed_circuit(request: SystemEvaluateRequest, nodes: list[str], system: str) -> bool:
    """路径是否首尾相接构成闭合环路

    只有闭合环路才同时含供程与回程，才能用来定水泵扬程。
    """
    if len(nodes) < 3:
        return False
    graph = _graph_for_system(request, system)
    return nodes[-1] in graph.get(nodes[0], [])


def _water_circuits(
    graph: dict[str, list[str]], starts: list[str], loads: set[str], max_paths: int
) -> list[list[str]]:
    """从冷/热源出发，枚举回到该源的闭合环路

    环路必须经过一个负荷节点（末端或冷却塔），否则"源—管—源"这种
    旁通回路也会被当成一条环路。
    """
    circuits: list[list[str]] = []
    seen: set[tuple[str, ...]] = set()
    for start in starts:
        _circuit_dfs(graph, start, start, [start], {start}, loads, circuits, seen, max_paths)
        if len(circuits) >= max_paths:
            break
    return circuits


def _circuit_dfs(
    graph: dict[str, list[str]],
    start: str,
    node: str,
    path: list[str],
    visited: set[str],
    loads: set[str],
    circuits: list[list[str]],
    seen: set[tuple[str, ...]],
    max_paths: int,
) -> None:
    if len(circuits) >= max_paths:
        return
    for neighbor in graph.get(node, []):
        if neighbor == start:
            if len(path) > 2 and (not loads or any(item in loads for item in path)):
                key = _circuit_key(path)
                if key not in seen:
                    seen.add(key)
                    circuits.append(path.copy())
                    if len(circuits) >= max_paths:
                        return
            continue
        if neighbor in visited:
            continue
        path.append(neighbor)
        visited.add(neighbor)
        _circuit_dfs(graph, start, neighbor, path, visited, loads, circuits, seen, max_paths)
        path.pop()
        visited.discard(neighbor)


def _circuit_key(path: list[str]) -> tuple[str, ...]:
    """同一环路正反两个方向的规范化键

    环路都从同一个源起头，故只需比较正序与「源 + 其余反序」，无需处理旋转。
    """
    forward = tuple(path)
    backward = (path[0],) + tuple(reversed(path[1:]))
    return min(forward, backward)


def pipe_ids_in_path(request: SystemEvaluateRequest, path: list[str], system: str) -> list[str]:
    pipes = {pipe.id for pipe in request.pipes if pipe.system == system}
    return [node for node in path if node in pipes]


def _graph_for_system(request: SystemEvaluateRequest, system: str) -> dict[str, list[str]]:
    known = {device.id for device in request.devices} | {pipe.id for pipe in request.pipes}
    graph: dict[str, list[str]] = defaultdict(list)
    for connection in request.connections:
        if connection.system != system or connection.from_id not in known or connection.to_id not in known:
            continue
        graph[connection.from_id].append(connection.to_id)
        graph[connection.to_id].append(connection.from_id)
    return {node: sorted(neighbors) for node, neighbors in graph.items()}


def _path_endpoints(request: SystemEvaluateRequest, system: str, graph: dict[str, list[str]]) -> tuple[list[str], list[str]]:
    roles = {device.id: role_for_device(device) for device in request.devices}
    if system == "chilled_water":
        return _nodes_with_role(roles, "cold_source"), _nodes_with_role(roles, "terminal")
    if system == "hot_water":
        terminals = _nodes_with_role(roles, "terminal")
        starts = _nodes_with_role(roles, "heat_source") or _nodes_with_role(roles, "cold_source")
        return starts, terminals
    if system == "cooling_water":
        return _nodes_with_role(roles, "cold_source"), _nodes_with_role(roles, "cooling_tower")
    if system == "air":
        terminals = _nodes_with_role(roles, "terminal")
        accessory_goals = [node for node, role in roles.items() if role in {"air_terminal", "air_accessory"}]
        return terminals, accessory_goals or [node for node in _open_nodes(graph) if node not in terminals]
    return [], []


def _nodes_with_role(roles: dict[str, str | None], role: str) -> list[str]:
    return sorted(node for node, node_role in roles.items() if node_role == role)


def _open_nodes(graph: dict[str, list[str]]) -> list[str]:
    return sorted(node for node, neighbors in graph.items() if len(neighbors) == 1)


def _dfs(graph: dict[str, list[str]], node: str, goals: set[str], path: list[str], paths: list[list[str]], max_paths: int) -> None:
    if len(paths) >= max_paths:
        return
    if node in goals and len(path) > 1:
        paths.append(path.copy())
        return
    if len(path) > max(len(graph) + 1, 2):
        return
    for neighbor in graph.get(node, []):
        if neighbor in path:
            continue
        path.append(neighbor)
        _dfs(graph, neighbor, goals, path, paths, max_paths)
        path.pop()


def _dedupe_paths(paths: list[list[str]]) -> list[list[str]]:
    deduped: list[list[str]] = []
    seen = set()
    for path in paths:
        key = tuple(path)
        reverse_key = tuple(reversed(path))
        if key in seen or reverse_key in seen:
            continue
        seen.add(key)
        deduped.append(path)
    return deduped
