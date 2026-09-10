from collections import defaultdict

from app.data.equipment import role_for_device
from app.schemas import SystemEvaluateRequest


def enumerate_network_paths(request: SystemEvaluateRequest, system: str, max_paths: int = 24) -> list[list[str]]:
    graph = _graph_for_system(request, system)
    if not graph:
        return []

    starts, goals = _path_endpoints(request, system, graph)
    if not starts:
        return []
    if not goals:
        goals = _open_nodes(graph) or [node for node in graph if node not in starts]

    paths: list[list[str]] = []
    for start in starts:
        _dfs(graph, start, set(goals), [start], paths, max_paths)
        if len(paths) >= max_paths:
            break
    return _dedupe_paths(paths)


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
