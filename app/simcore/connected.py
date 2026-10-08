"""Topology-aware orchestration of the existing device and room physics.

The session owns temperatures and controls. This module holds no mutable global
state; its network result is published with the same device/room snapshot.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import replace
from typing import TYPE_CHECKING

from app.data.rules import rule_value
from app.schemas import (
    Issue,
    RealtimeNetworkResult,
    RuntimeCircuitState,
    RuntimeNetworkNode,
)
from app.simcore.devices import (
    AIR_KW_PER_M3H_K,
    MIN_CHILLER_COP,
    ChillerState,
    CoolingTowerState,
    HeatSourceState,
    TerminalState,
    chiller_state,
    coil_leaving_state,
    cooling_tower_state,
    heat_source_cooling_state,
    heat_source_state,
    is_pump,
    is_terminal,
    is_valve,
    pump_state,
    terminal_state,
    valve_state,
)
from app.simcore.hydronics import (
    MAX_HYDRONIC_DELTA_T_C,
    WATER_KW_PER_M3H_K,
    HydronicThermalBranch,
    advance_temperature,
    mixed_return_temperature,
)
from app.simcore.network_flow import ElementKey, FlowDrive, FlowSolution, allocate_paths
from app.simcore.runtime_network import (
    GRAVITY,
    WATER_DENSITY,
    NetworkStatus,
    RuntimeNetworkLayout,
)
from app.unity_catalog import DeviceRole

if TYPE_CHECKING:
    from app.simcore.session import SimulationSession, _SolveResult
    from app.simcore.weather import Ambient


def _mean(values: list[tuple[float, float]], fallback: float) -> float:
    total = math.fsum(weight for _, weight in values)
    return (
        math.fsum(value * weight for value, weight in values) / total
        if total > 0.0
        else fallback
    )


def network_projection(
    layout: RuntimeNetworkLayout,
    flow: FlowSolution | None = None,
    resistance: Mapping[ElementKey, float] | None = None,
    temperatures: Mapping[str, tuple[float, float]] | None = None,
    extra_issues: tuple[Issue, ...] = (),
) -> RealtimeNetworkResult:
    """Project only the solved flows; never rediscover paths in a GET request."""
    element_flows = flow.element_flows if flow is not None else {}
    path_ids_by_node: dict[ElementKey, list[str]] = defaultdict(list)
    targets_by_node: dict[ElementKey, set[str]] = defaultdict(set)
    circuit_by_node = {}
    for compiled_circuit in layout.circuits:
        for node in compiled_circuit.node_ids:
            circuit_by_node[(compiled_circuit.system, node)] = compiled_circuit
    for path in layout.paths:
        for key in path.elements:
            path_ids_by_node[key].append(path.path_id)
            if path.system != "cooling_water":
                targets_by_node[key].add(
                    path.target_id if path.system != "air" else path.source_id
                )
    pipes: list[RuntimeNetworkNode] = []
    ducts: list[RuntimeNetworkNode] = []
    devices: list[RuntimeNetworkNode] = []
    for key, element in sorted(layout.elements.items()):
        q = element_flows.get(key, 0.0)
        circuit = circuit_by_node.get(key)
        supported = key in path_ids_by_node
        status: NetworkStatus = (
            "unsupported"
            if key in layout.unsupported_keys
            or (circuit is not None and circuit.status == "unsupported")
            else "modeled"
            if supported
            else "disconnected"
        )
        pair = (temperatures or {}).get(circuit.circuit_id) if circuit else None
        pressure = (resistance or {}).get(key, element.resistance) * q * q
        item = RuntimeNetworkNode(
            node_id=key[1],
            system=key[0],
            circuit_id=circuit.circuit_id if circuit else None,
            model_status=status,
            flow_m3_h=q,
            velocity_m_s=q / (3600.0 * element.area_m2) if element.area_m2 else None,
            pressure_drop_pa=pressure,
            head_m=pressure / (WATER_DENSITY * GRAVITY) if key[0] != "air" else None,
            diameter_mm=element.diameter_mm,
            supply_temp_c=pair[0] if pair else None,
            return_temp_c=pair[1] if pair else None,
            flow_blocked=flow is not None and q <= 0.0,
            served_terminal_ids=sorted(targets_by_node[key]),
            parameter_sources=list(element.parameter_sources),
        )
        (ducts if key[0] == "air" else pipes).append(
            item
        ) if element.pipe is not None else devices.append(item)
    circuits = []
    for circuit in layout.circuits:
        paths = [p for p in layout.paths if p.circuit_id == circuit.circuit_id]
        pair = (temperatures or {}).get(circuit.circuit_id)
        circuits.append(
            RuntimeCircuitState(
                circuit_id=circuit.circuit_id,
                system=circuit.system,
                source_ids=list(circuit.source_ids),
                drive_ids=list(circuit.drive_ids),
                model_status=circuit.status
                if paths or circuit.status != "modeled"
                else "disconnected",
                flow_m3_h=math.fsum(flow.path_flows[p.path_id] for p in paths)
                if flow
                else 0.0,
                supply_temp_c=pair[0] if pair else None,
                return_temp_c=pair[1] if pair else None,
            )
        )
    return RealtimeNetworkResult(
        circuits=circuits,
        pipes=pipes,
        ducts=ducts,
        devices=devices,
        issues=[*layout.issues, *extra_issues],
    )


def solve_connected(session: SimulationSession, ambient: Ambient) -> _SolveResult:
    # The import remains local: project/session adapters also import this module.
    from app.simcore.session import (
        FIXED_POINT_ITERATIONS,
        WATER_TEMPERATURE_TIME_CONSTANT_SECONDS,
        SimulationMode,
        _SolveResult,
    )

    layout = session.layout.network
    assert layout is not None
    nodes = {n.instance_id: n for n in session.layout.devices}
    rated = {key: session._operating_rated(node) for key, node in nodes.items()}
    cooling = session.config.mode is SimulationMode.COOLING
    heating = session.config.mode is SimulationMode.HEATING
    active_system = "chilled_water" if cooling else "hot_water" if heating else ""
    minimum_opening = float(rule_value("runtime_network.minimum_valve_ratio", 0.02))
    resistance = {key: element.resistance for key, element in layout.elements.items()}
    openings = {}
    for element_key, element in layout.elements.items():
        if element.valve:
            opening = (
                session._control(element_key[1], "opening_pct", 100.0) / 100.0
                if session._enabled(element_key[1])
                else 0.0
            )
            openings[element_key] = opening
            resistance[element_key] /= max(opening, minimum_opening) ** 2

    drives: dict[ElementKey, FlowDrive] = {}
    target_caps: dict[ElementKey, float] = {}
    reference_caps: dict[ElementKey, float] = {}
    source_caps: dict[ElementKey, float] = {}
    for path in layout.paths:
        spec = rated[path.drive_id]
        enabled = session._enabled(path.drive_id)
        if path.system == "air":
            ratio = (
                session._control(path.drive_id, "fan_speed_pct", 100.0) / 100.0
                if enabled
                else 0.0
            )
            q_ref = (spec.airflow_m3_h or 0.0) * layout.air_target_shares[
                path.target_id
            ]
            reference_caps[(path.system, path.target_id)] = q_ref
            cap = q_ref * ratio
            if (
                spec.role is DeviceRole.FRESH_AIR_UNIT
                and session.config.automatic_control
            ):
                required = math.fsum(
                    session._fresh_air_room_requirement(key)
                    for key in nodes[path.source_id].effective_room_ids
                )
                cap = min(cap, required * layout.air_target_shares[path.target_id])
            target_caps[(path.system, path.target_id)] = cap
            drives[(path.system, path.drive_id)] = FlowDrive(
                (spec.airflow_m3_h or 0.0) * ratio,
                layout.fan_pressure_pa[path.drive_id] * ratio**2,
            )
            continue
        pump = pump_state(
            spec,
            session._control(path.drive_id, "speed_pct", 100.0) / 100.0,
            enabled=enabled,
        )
        active = path.system == active_system or (
            path.system == "cooling_water" and cooling
        )
        drives[(path.system, path.drive_id)] = FlowDrive(
            pump.flow_m3_h if active else 0.0, pump.pressure_pa if active else 0.0
        )
        target_spec = rated[path.target_id]
        cap = target_spec.flow_m3_h or 0.0
        reference_caps[(path.system, path.target_id)] = cap
        if path.system != "cooling_water":
            cap *= session._control(path.target_id, "valve_opening_pct", 100.0) / 100.0
            if not nodes[path.target_id].effective_room_ids:
                cap = 0.0
        if not session._enabled(path.target_id):
            cap = 0.0
        target_caps[(path.system, path.target_id)] = cap
        source = rated[path.source_id]
        source_cap = (
            source.cooling_flow_m3_h
            if path.system == "cooling_water"
            else source.flow_m3_h
        )
        if source_cap is not None and source_cap > 0:
            source_caps[(path.system, path.source_id)] = source_cap
    flow = allocate_paths(
        layout.paths,
        resistance,
        drives,
        target_caps,
        reference_caps,
        openings,
        source_caps,
    )

    # Room distribution uses solved outlet flows. Base weights are independent
    # of the previous tick, including after reopening a previously closed duct.
    air_rooms: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for path in layout.paths:
        if path.system != "air":
            continue
        weights = dict(session._base_terminal_room_weights(nodes[path.source_id]))
        total = math.fsum(weights.get(key, 0.0) for key in path.room_ids)
        for room_id in path.room_ids:
            weight = (
                weights.get(room_id, 0.0) / total
                if total > 0
                else 1.0 / len(path.room_ids)
            )
            air_rooms[path.source_id][room_id] += flow.path_flows[path.path_id] * weight
    session._network_air_room_flows = {
        key: dict(value) for key, value in air_rooms.items()
    }

    water_circuits = [c for c in layout.circuits if c.system == active_system]
    terminal_circuit = {
        p.target_id: p.circuit_id for p in layout.paths if p.system == active_system
    }
    paths_by_circuit = {
        c.circuit_id: [p for p in layout.paths if p.circuit_id == c.circuit_id]
        for c in water_circuits
    }
    extra_issues = []
    source_capacity = {}
    for key, spec in rated.items():
        if spec.role not in {DeviceRole.CHILLER, DeviceRole.HEAT_SOURCE}:
            continue
        cap = spec.capacity_kw or 0.0
        source_flow = flow.source_flows.get((active_system, key), 0.0)
        cap = min(cap, WATER_KW_PER_M3H_K * source_flow * MAX_HYDRONIC_DELTA_T_C)
        if not session._enabled(key):
            cap = 0.0
        if cooling and key in layout.water_cooled_source_ids:
            condenser_flow = flow.source_flows.get(("cooling_water", key), 0.0)
            # Q_reject = Q_cold * (1 + 1/COP). Use the SAME 20 K bound as
            # cooling_tower_state. A fraction of nameplate condenser flow is
            # not a cooling-capacity fraction: part-load operation legitimately
            # uses less flow with a larger water temperature rise.
            reject_limit = WATER_KW_PER_M3H_K * condenser_flow * MAX_HYDRONIC_DELTA_T_C
            cap = min(cap, reject_limit / (1.0 + 1.0 / MIN_CHILLER_COP))
            if condenser_flow <= 0.0 and session._enabled(key):
                extra_issues.append(
                    Issue(
                        level="warning",
                        code="SIM_CONDENSER_FLOW_MISSING",
                        message="水冷冷机的冷却水未通流，制冷已联锁停止；请检查冷却水泵、阀门和冷却塔回路。",
                        target=key,
                    )
                )
                session._effective_controls[key]["enabled"] = 0.0
        source_capacity[key] = cap

    supply = {}
    capacity_by_circuit = {}
    for circuit in water_circuits:
        target_ids = {p.target_id for p in paths_by_circuit[circuit.circuit_id]}
        target_nodes = [nodes[key] for key in sorted(target_ids)]
        room_ids = {r for node in target_nodes for r in node.effective_room_ids}
        room_reference = _mean(
            [(session._room_temp(key), 1.0) for key in sorted(room_ids)],
            session.config.target_temp_c,
        )
        capacity = math.fsum(
            source_capacity.get(key, 0.0) for key in circuit.source_ids
        )
        capacity_by_circuit[circuit.circuit_id] = capacity
        sources = [
            nodes[key]
            for key in circuit.source_ids
            if source_capacity.get(key, 0.0) > 0.0
        ]
        target = (
            (
                session._supply_setpoint(sources)
                if cooling
                else session._hot_supply_setpoint(sources)
            )
            if capacity > 0.0
            else room_reference
        )
        current = session._network_supply_temps.get(circuit.circuit_id, room_reference)
        current = advance_temperature(
            current,
            target,
            session.config.step_seconds,
            WATER_TEMPERATURE_TIME_CONSTANT_SECONDS,
        )
        session._network_supply_temps[circuit.circuit_id] = current
        supply[circuit.circuit_id] = current

    terminals: dict[str, TerminalState] = {}
    for key, node in nodes.items():
        if not is_terminal(node.rated.role):
            continue
        circuit_id = terminal_circuit.get(key, "")
        available = capacity_by_circuit.get(circuit_id, 0.0) > 0.0
        fan_ratio = session._control(key, "fan_speed_pct", 100.0) / 100.0
        air = (
            (rated[key].airflow_m3_h or 0.0) * fan_ratio
            if key in layout.local_air_terminal_ids and session._enabled(key)
            else flow.drive_flows.get(("air", key), 0.0)
        )
        inlet, inlet_ratio = session._terminal_air_entering_state(node, air)
        state = terminal_state(
            rated[key],
            session._control(key, "valve_opening_pct", 100.0) / 100.0
            if active_system
            else 0.0,
            inlet,
            supply.get(circuit_id, inlet) if available else inlet,
            fan_speed_ratio=fan_ratio,
            enabled=session._enabled(key),
            actual_water_flow_m3_h=flow.target_flows.get((active_system, key), 0.0),
            actual_airflow_m3_h=air,
            room_humidity_ratio=inlet_ratio,
        )
        if not cooling:
            state = replace(state, cooling_kw=0.0)
        if not heating:
            state = replace(state, heating_kw=0.0)
        terminals[key] = state

    if cooling and session.config.design_cooling_load_kw is not None:
        delivered = math.fsum(state.cooling_kw for state in terminals.values())
        if delivered > session.config.design_cooling_load_kw:
            factor = session.config.design_cooling_load_kw / delivered
            terminals = {
                key: replace(state, cooling_kw=state.cooling_kw * factor)
                for key, state in terminals.items()
            }

    condenser = {key: ambient.wet_bulb_c + 5.0 for key in source_capacity}
    chillers: dict[str, ChillerState] = {}
    heat_sources: dict[str, HeatSourceState] = {}
    towers: dict[str, CoolingTowerState] = {}
    returns = {}
    for _ in range(FIXED_POINT_ITERATIONS):
        for circuit in water_circuits:
            ids = sorted({p.target_id for p in paths_by_circuit[circuit.circuit_id]})
            demand = math.fsum(
                terminals[key].cooling_kw if cooling else terminals[key].heating_kw
                for key in ids
            )
            capacity = capacity_by_circuit[circuit.circuit_id]
            for key in circuit.source_ids:
                cap = source_capacity.get(key, 0.0)
                load = min(demand * cap / capacity, cap) if capacity > 0.0 else 0.0
                source_flow = flow.source_flows.get((active_system, key), 0.0)
                # Hydraulic availability caps delivered load, not the rated
                # capacity used in PLR/COP calibration.
                spec = rated[key]
                if rated[key].role is DeviceRole.CHILLER:
                    result = chiller_state(
                        spec,
                        load,
                        supply[circuit.circuit_id],
                        condenser[key],
                        source_flow,
                        enabled=cooling and cap > 0.0,
                    )
                    chillers[key] = replace(result, flow_m3_h=source_flow)
                elif cooling:
                    result_heat = heat_source_cooling_state(
                        spec,
                        load,
                        supply[circuit.circuit_id],
                        ambient.dry_bulb_c,
                        chilled_flow_m3_h=source_flow,
                        enabled=cap > 0.0,
                    )
                    heat_sources[key] = replace(result_heat, flow_m3_h=source_flow)
                else:
                    result_heat = heat_source_state(
                        spec,
                        load,
                        supply[circuit.circuit_id],
                        ambient.dry_bulb_c,
                        hot_water_flow_m3_h=source_flow,
                        enabled=cap > 0.0,
                    )
                    heat_sources[key] = replace(result_heat, flow_m3_h=source_flow)
            output = math.fsum(
                (
                    chillers[key].cooling_kw
                    if key in chillers
                    else heat_sources[key].cooling_kw
                    if cooling
                    else heat_sources[key].heating_kw
                )
                for key in circuit.source_ids
            )
            if demand > output and demand > 0:
                scale = output / demand
                for key in ids:
                    terminals[key] = replace(
                        terminals[key],
                        cooling_kw=terminals[key].cooling_kw * scale,
                        heating_kw=terminals[key].heating_kw * scale,
                    )
            returns[circuit.circuit_id] = mixed_return_temperature(
                supply[circuit.circuit_id],
                (
                    HydronicThermalBranch(
                        key,
                        terminals[key].water_flow_m3_h,
                        terminals[key].cooling_kw
                        if cooling
                        else terminals[key].heating_kw,
                    )
                    for key in ids
                ),
                heating=heating,
            )
            for key in circuit.source_ids:
                if key in chillers:
                    chillers[key] = replace(
                        chillers[key], return_temp_c=returns[circuit.circuit_id]
                    )
                elif key in heat_sources:
                    heat_sources[key] = replace(
                        heat_sources[key], return_temp_c=returns[circuit.circuit_id]
                    )

        tower_loads: dict[str, float] = defaultdict(float)
        for path in layout.paths:
            if path.system != "cooling_water" or path.source_id not in chillers:
                continue
            q = flow.source_flows.get(("cooling_water", path.source_id), 0.0)
            if q > 0.0:
                tower_loads[path.target_id] += (
                    chillers[path.source_id].heat_reject_kw
                    * flow.path_flows[path.path_id]
                    / q
                )
        for key, spec in rated.items():
            if spec.role is DeviceRole.COOLING_TOWER:
                towers[key] = cooling_tower_state(
                    spec,
                    session._control(key, "fan_speed_pct", 100.0) / 100.0,
                    tower_loads[key],
                    ambient.wet_bulb_c,
                    water_flow_m3_h=flow.target_flows.get(("cooling_water", key), 0.0),
                    enabled=session._enabled(key),
                )
        for key in condenser:
            pairs = [
                (towers[p.target_id].entering_water_temp_c, flow.path_flows[p.path_id])
                for p in layout.paths
                if p.system == "cooling_water" and p.source_id == key
            ]
            condenser[key] = _mean(pairs, ambient.dry_bulb_c)

    for key, state in terminals.items():
        inlet, humidity_ratio = session._terminal_air_entering_state(
            nodes[key], state.airflow_m3_h
        )
        outlet = inlet
        if state.cooling_kw > 0.0:
            outlet, _ = coil_leaving_state(
                inlet, humidity_ratio, state.airflow_m3_h, state.cooling_kw
            )
        elif state.heating_kw > 0.0 and state.airflow_m3_h > 0.0:
            outlet += state.heating_kw / (AIR_KW_PER_M3H_K * state.airflow_m3_h)
        terminals[key] = replace(state, supply_air_temp_c=outlet)

    for key, spec in rated.items():
        if spec.role is DeviceRole.CHILLER and key not in chillers:
            chillers[key] = chiller_state(
                spec,
                0.0,
                session.config.target_temp_c,
                ambient.dry_bulb_c,
                0.0,
                enabled=False,
            )
        if spec.role is DeviceRole.HEAT_SOURCE and key not in heat_sources:
            heat_sources[key] = heat_source_state(
                spec,
                0.0,
                session.config.target_temp_c,
                ambient.dry_bulb_c,
                0.0,
                enabled=False,
            )
    pumps = {
        key: pump_state(
            spec,
            session._control(key, "speed_pct", 100.0) / 100.0,
            enabled=session._enabled(key),
            actual_flow_m3_h=math.fsum(
                flow.drive_flows.get((system, key), 0.0)
                for system in ("chilled_water", "cooling_water", "hot_water")
            ),
        )
        for key, spec in rated.items()
        if is_pump(spec.role)
    }
    temperatures = {
        key: (value, returns.get(key, value)) for key, value in supply.items()
    }
    for circuit in layout.circuits:
        if circuit.system == "cooling_water":
            qs = [
                (towers[p.target_id].leaving_water_temp_c, flow.path_flows[p.path_id])
                for p in layout.paths
                if p.circuit_id == circuit.circuit_id and p.target_id in towers
            ]
            qr = [
                (towers[p.target_id].entering_water_temp_c, flow.path_flows[p.path_id])
                for p in layout.paths
                if p.circuit_id == circuit.circuit_id and p.target_id in towers
            ]
            temperatures[circuit.circuit_id] = (
                _mean(qs, ambient.wet_bulb_c),
                _mean(qr, ambient.wet_bulb_c),
            )
        elif circuit.system == "air" and circuit.source_ids[0] in terminals:
            key = circuit.source_ids[0]
            temperatures[circuit.circuit_id] = (
                terminals[key].supply_air_temp_c,
                session._terminal_room_reference_temp(nodes[key]),
            )
    network = network_projection(
        layout, flow, resistance, temperatures, tuple(extra_issues)
    )
    total_flow = {
        system: math.fsum(
            value for (medium, _), value in flow.drive_flows.items() if medium == system
        )
        for system in ("chilled_water", "cooling_water", "hot_water")
    }
    mean_supply = _mean(
        [
            (
                supply[c.circuit_id],
                math.fsum(
                    flow.path_flows[p.path_id] for p in paths_by_circuit[c.circuit_id]
                ),
            )
            for c in water_circuits
        ],
        session.config.target_temp_c,
    )
    mean_return = _mean(
        [
            (
                returns[c.circuit_id],
                math.fsum(
                    flow.path_flows[p.path_id] for p in paths_by_circuit[c.circuit_id]
                ),
            )
            for c in water_circuits
        ],
        mean_supply,
    )
    return _SolveResult(
        supply_water_temp_c=mean_supply if cooling else session._chilled_supply_temp_c,
        return_water_temp_c=mean_return if cooling else session._chilled_supply_temp_c,
        condenser_temp_c=_mean(
            [
                (value, flow.source_flows.get(("cooling_water", key), 0.0))
                for key, value in condenser.items()
            ],
            ambient.wet_bulb_c,
        ),
        chilled_flow_m3_h=total_flow["chilled_water"],
        cooling_flow_m3_h=total_flow["cooling_water"],
        hot_water_flow_m3_h=total_flow["hot_water"],
        hot_water_supply_temp_c=mean_supply if heating else session._hot_supply_temp_c,
        hot_water_return_temp_c=mean_return if heating else session._hot_supply_temp_c,
        cooling_load_kw=math.fsum(s.cooling_kw for s in terminals.values()),
        heating_load_kw=math.fsum(s.heating_kw for s in terminals.values()),
        design_load_kw=session._terminal_design_load(
            [n for n in nodes.values() if is_terminal(n.rated.role)]
        )
        if cooling
        else 0.0,
        design_heating_load_kw=session._terminal_design_load(
            [n for n in nodes.values() if is_terminal(n.rated.role)]
        )
        if heating
        else 0.0,
        installed_capacity_kw=math.fsum(
            (spec.capacity_kw or 0.0)
            for key, spec in rated.items()
            if spec.role in {DeviceRole.CHILLER, DeviceRole.HEAT_SOURCE}
            and session._enabled(key)
        )
        if cooling
        else 0.0,
        installed_heating_capacity_kw=math.fsum(
            (spec.capacity_kw or 0.0)
            for key, spec in rated.items()
            if spec.role is DeviceRole.HEAT_SOURCE and session._enabled(key)
        )
        if heating
        else 0.0,
        chillers=chillers,
        heat_sources=heat_sources,
        pumps=pumps,
        towers=towers,
        terminals=terminals,
        valves={
            key: valve_state(
                session._control(key, "opening_pct", 100.0) / 100.0,
                session._enabled(key),
            )
            for key, spec in rated.items()
            if is_valve(spec.role)
        },
        network=network,
    )
