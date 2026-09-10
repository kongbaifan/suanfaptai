from collections import defaultdict, deque

from app.data.equipment import role_for_device, systems_for_device
from app.schemas import Issue, SystemEvaluateRequest


WATER_SYSTEMS = {"chilled_water", "cooling_water", "hot_water"}


def validate_topology(request: SystemEvaluateRequest) -> list[Issue]:
    if not request.connections:
        return []

    issues: list[Issue] = []
    devices = {device.id: device for device in request.devices}
    pipes = {pipe.id: pipe for pipe in request.pipes}
    known_ids = set(devices) | set(pipes)
    adjacency: dict[str, list[tuple[str, str]]] = defaultdict(list)
    edge_count_by_system: dict[str, int] = defaultdict(int)

    for connection in request.connections:
        if connection.from_id not in known_ids or connection.to_id not in known_ids:
            issues.append(
                Issue(
                    level="error",
                    code="UNKNOWN_CONNECTION_NODE",
                    message=f"Connection references unknown node {connection.from_id}->{connection.to_id}.",
                )
            )
            continue

        adjacency[connection.from_id].append((connection.to_id, connection.system))
        adjacency[connection.to_id].append((connection.from_id, connection.system))
        edge_count_by_system[connection.system] += 1
        issues.extend(_validate_endpoint_system(connection.from_id, connection.system, devices, pipes))
        issues.extend(_validate_endpoint_system(connection.to_id, connection.system, devices, pipes))

    for device_id, device in devices.items():
        if device_id not in adjacency:
            issues.append(
                Issue(
                    level="warning",
                    code="ISOLATED_DEVICE",
                    message=f"Device {device_id} is not connected to any topology edge.",
                    target=device_id,
                )
            )
        elif not systems_for_device(device):
            issues.append(
                Issue(
                    level="info",
                    code="UNKNOWN_DEVICE_CATALOG",
                    message=f"Device {device_id} has no equipment catalog entry; system compatibility was not checked.",
                    target=device_id,
                )
            )

    for pipe_id in pipes:
        if pipe_id not in adjacency:
            issues.append(
                Issue(
                    level="warning",
                    code="ISOLATED_PIPE",
                    message=f"Pipe {pipe_id} is not connected to any topology edge.",
                    target=pipe_id,
                )
            )

    issues.extend(_validate_required_paths(devices, adjacency))
    issues.extend(_validate_water_loop_closure(adjacency, edge_count_by_system))
    return _dedupe_issues(issues)


def _validate_endpoint_system(node_id, system, devices, pipes) -> list[Issue]:
    if node_id in pipes:
        pipe = pipes[node_id]
        if pipe.system != system:
            return [
                Issue(
                    level="error",
                    code="PIPE_SYSTEM_MISMATCH",
                    message=f"Pipe {node_id} belongs to {pipe.system} but is connected as {system}.",
                    target=node_id,
                )
            ]
        return []

    device = devices[node_id]
    allowed_systems = systems_for_device(device)
    if allowed_systems and system not in allowed_systems:
        return [
            Issue(
                level="error",
                code="DEVICE_SYSTEM_MISMATCH",
                message=f"Device {node_id} ({device.type}) does not support {system}.",
                target=node_id,
            )
        ]
    return []


def _validate_required_paths(devices, adjacency) -> list[Issue]:
    issues: list[Issue] = []
    roles_by_id = {device_id: role_for_device(device) for device_id, device in devices.items()}

    chilled_components = _components_for_system("chilled_water", adjacency)
    if _has_role(roles_by_id, "cold_source") and _has_role(roles_by_id, "pump") and _has_role(roles_by_id, "terminal"):
        if not any(_component_has_roles(component, roles_by_id, {"cold_source", "pump", "terminal"}) for component in chilled_components):
            issues.append(
                Issue(
                    level="error",
                    code="INCOMPLETE_CHILLED_WATER_PATH",
                    message="Chilled-water topology does not connect cold source, pump, and terminal equipment in one path.",
                )
            )

    cooling_components = _components_for_system("cooling_water", adjacency)
    if _has_role(roles_by_id, "cold_source") and _has_role(roles_by_id, "cooling_tower"):
        if not any(_component_has_roles(component, roles_by_id, {"cold_source", "cooling_tower"}) for component in cooling_components):
            issues.append(
                Issue(
                    level="warning",
                    code="INCOMPLETE_COOLING_WATER_PATH",
                    message="Cooling-water topology does not connect cold source and cooling tower.",
                )
            )

    for device_id, role in roles_by_id.items():
        systems = {system for _, system in adjacency.get(device_id, [])}
        if role == "pump" and not (systems & WATER_SYSTEMS):
            issues.append(Issue(level="warning", code="PUMP_NOT_IN_WATER_LOOP", message=f"Pump {device_id} is not connected to a water loop.", target=device_id))
        if role == "terminal" and not (systems & {"chilled_water", "hot_water", "air"}):
            issues.append(Issue(level="warning", code="TERMINAL_NOT_CONNECTED", message=f"Terminal {device_id} is not connected to a service system.", target=device_id))

    return issues


def _validate_water_loop_closure(adjacency, edge_count_by_system) -> list[Issue]:
    issues: list[Issue] = []
    for system in WATER_SYSTEMS:
        if edge_count_by_system[system] == 0:
            continue
        system_nodes = {node for node, edges in adjacency.items() if any(edge_system == system for _, edge_system in edges)}
        if not system_nodes:
            continue
        if edge_count_by_system[system] < len(system_nodes):
            issues.append(
                Issue(
                    level="warning",
                    code="OPEN_WATER_LOOP",
                    message=f"{system} topology appears open; add return-side connections to form a closed loop.",
                    target=system,
                )
            )
    return issues


def _components_for_system(system: str, adjacency) -> list[set[str]]:
    visited: set[str] = set()
    components: list[set[str]] = []
    system_nodes = {node for node, edges in adjacency.items() if any(edge_system == system for _, edge_system in edges)}

    for start in system_nodes:
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


def _has_role(roles_by_id, role: str) -> bool:
    return any(item_role == role for item_role in roles_by_id.values())


def _component_has_roles(component: set[str], roles_by_id, required_roles: set[str]) -> bool:
    found = {roles_by_id.get(node) for node in component}
    return required_roles <= found


def _dedupe_issues(issues: list[Issue]) -> list[Issue]:
    deduped: list[Issue] = []
    seen = set()
    for issue in issues:
        key = (issue.level, issue.code, issue.message, issue.target)
        if key not in seen:
            seen.add(key)
            deduped.append(issue)
    return deduped

