"""Project DTO adapter for the shared simulation core."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace

from app.calculators.load import calculate_load
from app.data.equipment import resolve_device_type
from app.room_bindings import (
    raise_for_room_binding_errors,
    resolve_room_bindings,
    room_instance_id,
)
from app.schemas import (
    DeviceInput,
    Issue,
    LoadRequest,
    PipeInput,
    RoomInput,
    SystemEvaluateRequest,
)
from app.simcore.hydronics import HydronicBranch, HydronicLoop, pipe_resistance_index
from app.simcore.rated import RatedSpec, resolve_rated_spec_by_type
from app.simcore.room import RoomThermalConfig
from app.simcore.session import DeviceNode, PlantLayout, RoomNode, WaterLoop
from app.unity_catalog import DeviceRole

THERMAL_MASS_KJ_PER_M2_K = 80.0


@dataclass(frozen=True, slots=True)
class ProjectSimulationLayout:
    layout: PlantLayout
    rated_by_device: Mapping[str, RatedSpec]
    # Binding/topology diagnostics are retained with the resolved layout so the
    # realtime adapter can expose them on the first snapshot without rerunning a
    # static evaluator.  All callers keep strict room-binding validation;
    # runtime callers additionally request non-blocking topology diagnostics.
    issues: tuple[Issue, ...] = ()


def build_project_layout(project: SystemEvaluateRequest) -> ProjectSimulationLayout:
    """Resolve project equipment once and build the strict simcore layout.

    This is the historical adapter used by static evaluation and save/validate
    paths.  Invalid room bindings remain a client-facing 422 through the
    existing ``ProjectRoomBindingError`` contract.
    """
    return _build_project_layout(project)


def build_runtime_layout(project: SystemEvaluateRequest) -> ProjectSimulationLayout:
    """Build a teaching runtime layout without requiring complete topology.

    The realtime teaching path must be able to start with an incomplete design
    (for example, a room with no terminal or a disconnected water loop).  Such
    projects still get a bounded simcore layout and retain non-fatal binding
    diagnostics on the returned object.  Unknown/duplicate/unsupported room
    references remain structural input errors and are rejected with the same
    ``ProjectRoomBindingError`` used by the strict adapter.

    Persisted project writes and static evaluation continue to call the strict
    ``build_project_layout`` entry point, so their established 422 semantics do
    not change.
    """
    return _build_project_layout(project, runtime_diagnostics=True)


def _build_project_layout(
    project: SystemEvaluateRequest,
    *,
    runtime_diagnostics: bool = False,
) -> ProjectSimulationLayout:
    """Shared project-to-simcore adapter implementation."""
    rated_by_device = {
        device.id: resolve_rated_spec_by_type(
            resolve_device_type(device.type), device.name
        )
        for device in project.devices
    }
    binding_resolution = resolve_room_bindings(
        project,
        roles_by_device={
            device_id: rated.role for device_id, rated in rated_by_device.items()
        },
    )
    # Room binding errors are structural input errors for every caller.  The
    # runtime-specific difference is limited to non-blocking topology warnings.
    raise_for_room_binding_errors(binding_resolution)
    load_result = calculate_load(
        LoadRequest(
            rooms=project.rooms,
            floors=project.floors,
            simultaneity_factor=project.simultaneity_factor,
            safety_factor=project.safety_factor,
        )
    )
    loads_by_room_id = {
        room_instance_id(project.rooms[index], index): room_load
        for index, room_load in enumerate(load_result.rooms)
    }
    room_nodes = tuple(
        RoomNode(
            room_id=room_instance_id(room, index),
            name=room.name,
            thermal=_room_thermal_config(room),
            initial_relative_humidity_pct=room.indoor_relative_humidity * 100.0,
            design_cooling_kw=loads_by_room_id[room_instance_id(room, index)].cooling_kw,
            design_heating_kw=loads_by_room_id[room_instance_id(room, index)].heating_kw,
            fresh_air_m3_h=loads_by_room_id[
                room_instance_id(room, index)
            ].fresh_air_m3_h,
        )
        for index, room in enumerate(project.rooms)
    )
    device_nodes = tuple(
        DeviceNode(
            instance_id=device.id,
            rated=_effective_rated(device, rated_by_device[device.id]),
            quantity=device.quantity,
            room_id=(binding_resolution.room_ids_by_device[device.id] or (None,))[0],
            served_room_ids=binding_resolution.room_ids_by_device[device.id],
            water_loop=_pump_loop(device, rated_by_device[device.id].role, project),
        )
        for device in project.devices
    )
    modeled_water_loops = _modeled_water_loops(project)
    hydronic_branches = _hydronic_branches(project, device_nodes, modeled_water_loops)
    issues = list(binding_resolution.issues)
    if runtime_diagnostics:
        issues.extend(
            _runtime_topology_issues(
                project,
                device_nodes,
                modeled_water_loops,
                hydronic_branches,
            )
        )
    return ProjectSimulationLayout(
        layout=PlantLayout(
            devices=device_nodes,
            rooms=room_nodes,
            hydronic_branches=hydronic_branches,
            modeled_water_loops=frozenset(modeled_water_loops),
        ),
        rated_by_device={node.instance_id: node.rated for node in device_nodes},
        issues=tuple(_dedupe_issues(issues)),
    )


def _runtime_topology_issues(
    project: SystemEvaluateRequest,
    device_nodes: tuple[DeviceNode, ...],
    modeled_water_loops: set[HydronicLoop],
    hydronic_branches: tuple[HydronicBranch, ...],
) -> tuple[Issue, ...]:
    """Return lightweight, non-blocking diagnostics for the runtime adapter.

    This deliberately does not call the PortGraph or Hardy-Cross solvers.  It
    only checks whether a declared pipe is attached to any project edge and
    whether a terminal that declares a chilled/hot-water edge reached a
    serviceable branch.  The resulting warnings make an incomplete student
    design visible while leaving the bounded simcore tick available.
    """
    known_ids = {node.instance_id for node in device_nodes} | {
        pipe.id for pipe in project.pipes
    }
    connected_ids = {
        node_id
        for connection in project.connections
        if connection.from_id in known_ids and connection.to_id in known_ids
        for node_id in (connection.from_id, connection.to_id)
    }
    issues: list[Issue] = [
        Issue(
            level="warning",
            code="ISOLATED_PIPE",
            message=f"Pipe {pipe.id} is not connected to any topology edge.",
            target=pipe.id,
        )
        for pipe in project.pipes
        if pipe.id not in connected_ids
    ]

    terminal_roles = {
        DeviceRole.FCU,
        DeviceRole.AHU,
        DeviceRole.FRESH_AIR_UNIT,
    }
    branch_keys = {
        (branch.loop, branch.terminal_instance_id)
        for branch in hydronic_branches
    }
    for node in device_nodes:
        if node.rated.role not in terminal_roles:
            continue
        declared_water_loops = {
            "chilled"
            if connection.system == "chilled_water"
            else "hot"
            for connection in project.connections
            if connection.system in {"chilled_water", "hot_water"}
            and node.instance_id in {connection.from_id, connection.to_id}
        }
        for loop in sorted(declared_water_loops & modeled_water_loops):
            if (loop, node.instance_id) in branch_keys:
                continue
            system = "chilled_water" if loop == "chilled" else "hot_water"
            issues.append(
                Issue(
                    level="warning",
                    code="TERMINAL_WATER_UNREACHABLE",
                    message=(
                        f"Terminal {node.instance_id} is not reachable from the "
                        f"serviceable {system} loop."
                    ),
                    target=node.instance_id,
                )
            )
    return tuple(issues)

def project_humidity_pct(project: SystemEvaluateRequest) -> float:
    if not project.rooms:
        return 70.0
    return (
        sum(room.outdoor_relative_humidity for room in project.rooms)
        / len(project.rooms)
        * 100.0
    )


def _effective_rated(device: DeviceInput, rated: RatedSpec) -> RatedSpec:
    """Apply explicit project values to the per-unit selected-model spec."""
    overridable = {
        "capacity_kw",
        "power_kw",
        "flow_m3_h",
        "cooling_flow_m3_h",
        "airflow_m3_h",
        "fan_power_kw",
        "head_m",
    }
    values: dict[str, float | None] = {}
    degraded = list(rated.degraded_fields)
    for field in overridable:
        value = getattr(rated, field)
        override = getattr(device, field, None)
        if override is not None:
            value = float(override)
            degraded.append(f"project_override:{field}")
        values[field] = value
    return replace(
        rated,
        capacity_kw=values["capacity_kw"],
        power_kw=values["power_kw"],
        flow_m3_h=values["flow_m3_h"],
        cooling_flow_m3_h=values["cooling_flow_m3_h"],
        airflow_m3_h=values["airflow_m3_h"],
        fan_power_kw=values["fan_power_kw"],
        head_m=values["head_m"],
        degraded_fields=tuple(dict.fromkeys(degraded)),
    )


def _room_thermal_config(room: RoomInput) -> RoomThermalConfig:
    return RoomThermalConfig(
        thermal_capacity_kj_k=max(room.area_m2 * THERMAL_MASS_KJ_PER_M2_K, 1.0),
        envelope_conductance_kw_k=(
            room.envelope_u_w_m2k * room.area_m2 * room.exposed_area_factor / 1000.0
        ),
        occupancy_people=room.people,
        lighting_w=room.lighting_w_m2 * room.area_m2,
        equipment_w=room.equipment_w_m2 * room.area_m2,
        volume_m3=max(room.area_m2 * room.height_m, 1.0),
    )


def _pump_loop(
    device: DeviceInput, role: DeviceRole, project: SystemEvaluateRequest
) -> WaterLoop | None:
    if role not in {
        DeviceRole.PUMP,
        DeviceRole.PUMP_CHILLED_WATER,
        DeviceRole.PUMP_COOLING_WATER,
    }:
        return None
    media = {
        connection.system
        for connection in project.connections
        if connection.from_id == device.id or connection.to_id == device.id
    }
    if "cooling_water" in media and "chilled_water" not in media:
        return "cooling"
    if "hot_water" in media and not ({"chilled_water", "cooling_water"} & media):
        return "hot"
    return "chilled"


def _modeled_water_loops(project: SystemEvaluateRequest) -> set[HydronicLoop]:
    systems = {pipe.system for pipe in project.pipes} | {
        connection.system for connection in project.connections
    }
    modeled: set[HydronicLoop] = set()
    if "chilled_water" in systems:
        modeled.add("chilled")
    if "hot_water" in systems:
        modeled.add("hot")
    return modeled


def _hydronic_branches(
    project: SystemEvaluateRequest,
    device_nodes: tuple[DeviceNode, ...],
    modeled_water_loops: set[HydronicLoop],
) -> tuple[HydronicBranch, ...]:
    pipe_by_id = {pipe.id: pipe for pipe in project.pipes}
    branches: list[HydronicBranch] = []
    terminal_roles = {DeviceRole.FCU, DeviceRole.AHU, DeviceRole.FRESH_AIR_UNIT}
    for loop in sorted(modeled_water_loops):
        system = "chilled_water" if loop == "chilled" else "hot_water"
        serviceable_nodes = _serviceable_water_nodes(
            project, device_nodes, loop, system
        )
        for node in device_nodes:
            if node.rated.role not in terminal_roles:
                continue
            if node.instance_id not in serviceable_nodes:
                continue
            connected_pipes = _direct_terminal_pipes(
                node.instance_id, system, project, pipe_by_id
            )
            resistance = sum(
                pipe_resistance_index(
                    pipe.length_m,
                    pipe.diameter_mm,
                    pipe.local_resistance_coefficient,
                )
                for pipe in connected_pipes
            )
            branches.append(
                HydronicBranch(
                    terminal_instance_id=node.instance_id,
                    loop=loop,
                    resistance_index=resistance if resistance > 0.0 else 1.0,
                )
            )
    return tuple(
        sorted(branches, key=lambda branch: (branch.loop, branch.terminal_instance_id))
    )


def _serviceable_water_nodes(
    project: SystemEvaluateRequest,
    device_nodes: tuple[DeviceNode, ...],
    loop: HydronicLoop,
    system: str,
) -> set[str]:
    """Return nodes in components that can be served by this water loop."""
    known_ids = {node.instance_id for node in device_nodes} | {
        pipe.id for pipe in project.pipes
    }
    adjacency: dict[str, set[str]] = {}
    for connection in project.connections:
        if (
            connection.system != system
            or connection.from_id not in known_ids
            or connection.to_id not in known_ids
        ):
            continue
        adjacency.setdefault(connection.from_id, set()).add(connection.to_id)
        adjacency.setdefault(connection.to_id, set()).add(connection.from_id)

    pump_roles = {
        DeviceRole.PUMP,
        DeviceRole.PUMP_CHILLED_WATER,
        DeviceRole.PUMP_COOLING_WATER,
    }
    pump_ids = {
        node.instance_id
        for node in device_nodes
        if node.rated.role in pump_roles and node.water_loop == loop
    }
    # chilled 环路的源锚包含冷机与热泵：热泵制冷工况换向后即冷源
    # （session 容量池同口径），只连热泵的末端组件不应被判不可服务。
    # hot 环路源锚仅热泵。
    source_roles = (
        {DeviceRole.CHILLER, DeviceRole.HEAT_SOURCE}
        if loop == "chilled"
        else {DeviceRole.HEAT_SOURCE}
    )
    source_ids = {
        node.instance_id for node in device_nodes if node.rated.role in source_roles
    }

    serviceable: set[str] = set()
    visited: set[str] = set()
    for start in sorted(adjacency):
        if start in visited:
            continue
        component: set[str] = set()
        pending = [start]
        visited.add(start)
        while pending:
            current = pending.pop()
            component.add(current)
            for neighbor in sorted(adjacency.get(current, ())):
                if neighbor not in visited:
                    visited.add(neighbor)
                    pending.append(neighbor)
        has_required_pump = not pump_ids or bool(component & pump_ids)
        has_required_source = not source_ids or bool(component & source_ids)
        if has_required_pump and has_required_source:
            serviceable.update(component)
    return serviceable


def _direct_terminal_pipes(
    terminal_instance_id: str,
    system: str,
    project: SystemEvaluateRequest,
    pipe_by_id: Mapping[str, PipeInput],
) -> tuple[PipeInput, ...]:
    pipe_ids: set[str] = set()
    for connection in project.connections:
        if connection.system != system:
            continue
        if connection.from_id == terminal_instance_id:
            pipe_ids.add(connection.to_id)
        elif connection.to_id == terminal_instance_id:
            pipe_ids.add(connection.from_id)
    return tuple(
        pipe_by_id[pipe_id]
        for pipe_id in sorted(pipe_ids)
        if pipe_id in pipe_by_id and pipe_by_id[pipe_id].system == system
    )


def _dedupe_issues(issues: list[Issue]) -> tuple[Issue, ...]:
    deduped: list[Issue] = []
    seen: set[tuple[str, str, str, str | None]] = set()
    for issue in issues:
        key = (issue.level, issue.code, issue.message, issue.target)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(issue)
    return tuple(deduped)
