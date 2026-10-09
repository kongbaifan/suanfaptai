"""Connection-driven runtime regressions; all projects are isolated fixtures.

These cases exercise physical consequences of edits, not merely diagnostics or
the presence of response fields. Closed water circuits and explicit air outlets
make the intended boundary conditions independent of catalogue defaults.
"""

from copy import deepcopy
from dataclasses import replace

import pytest

from app.samples import SAMPLES
from app.schemas import ConnectionInput, DeviceInput, PipeInput, SystemEvaluateRequest
from app.simcore.project import build_runtime_layout
from app.simcore.session import SimulationConfig, SimulationMode, SimulationSession


@pytest.fixture
def water_project() -> SystemEvaluateRequest:
    return SystemEvaluateRequest.model_validate(
        {
            "rooms": [
                {"id": "r1", "area_m2": 60, "people": 8, "device_ids": ["fcu1"]},
                {"id": "r2", "area_m2": 60, "people": 8, "device_ids": ["fcu2"]},
            ],
            "devices": [
                {"id": "source", "type": "空气源热泵", "name": "SHAF-80H"},
                {"id": "pump", "type": "循环泵", "name": "TPE3 65-150"},
                {"id": "fcu1", "type": "风机盘管", "name": "FP-204WA"},
                {"id": "fcu2", "type": "风机盘管", "name": "FP-204WA"},
            ],
            "pipes": [
                {
                    "id": name,
                    "system": "chilled_water",
                    "pipe_type": kind,
                    "diameter_mm": 65,
                    "length_m": 10,
                }
                for name, kind in [
                    ("main", "main_supply"),
                    ("return", "main_return"),
                    ("s1", "branch_supply"),
                    ("s2", "branch_supply"),
                    ("rtn1", "branch_return"),
                    ("rtn2", "branch_return"),
                ]
            ],
            "connections": [
                {"from_id": a, "to_id": b, "system": "chilled_water"}
                for a, b in [
                    ("source", "pump"),
                    ("pump", "main"),
                    ("main", "s1"),
                    ("main", "s2"),
                    ("s1", "fcu1"),
                    ("s2", "fcu2"),
                    ("fcu1", "rtn1"),
                    ("fcu2", "rtn2"),
                    ("rtn1", "return"),
                    ("rtn2", "return"),
                    ("return", "source"),
                ]
            ],
        }
    )


def run_project(project, controls=None, *, mode=SimulationMode.COOLING, ticks=30):
    session = SimulationSession(
        build_runtime_layout(project).layout,
        SimulationConfig(
            step_seconds=60,
            fixed_hour_of_day=14,
            initial_room_temp_c=30,
            mode=mode,
            automatic_control=False,
        ),
    )
    for device_id, values in (controls or {}).items():
        session.set_controls(device_id, values)
    for _ in range(ticks):
        session.step()
    return session


def insert_valve(project, from_id, to_id, valve_id="valve"):
    project.devices.append(DeviceInput(id=valve_id, type="球阀", name="Q11F-16T DN25"))
    edge = next(
        e for e in project.connections if e.from_id == from_id and e.to_id == to_id
    )
    project.connections.remove(edge)
    project.connections.extend(
        [
            ConnectionInput(from_id=from_id, to_id=valve_id, system=edge.system),
            ConnectionInput(from_id=valve_id, to_id=to_id, system=edge.system),
        ]
    )


@pytest.mark.parametrize("edge", [("s1", "fcu1"), ("fcu1", "rtn1")])
@pytest.mark.parametrize(
    "system,mode",
    [("chilled_water", SimulationMode.COOLING), ("hot_water", SimulationMode.HEATING)],
)
def test_supply_and_return_valves_stop_only_their_branch(
    water_project, edge, system, mode
):
    for pipe in water_project.pipes:
        pipe.system = system
    for connection in water_project.connections:
        connection.system = system
    insert_valve(water_project, *edge)
    opened = run_project(
        water_project, {"valve": {"opening_pct": 100}}, mode=mode
    ).snapshot
    session = run_project(water_project, {"valve": {"opening_pct": 0}}, mode=mode)
    closed = session.snapshot
    assert opened.devices["fcu1"].values["water_flow_m3_h"] > 0
    assert closed.devices["fcu1"].values["water_flow_m3_h"] == 0
    assert closed.devices["fcu1"].values["cooling_kw"] == 0
    assert closed.devices["fcu1"].values["heating_kw"] == 0
    assert closed.devices["fcu2"].values["water_flow_m3_h"] > 0
    assert closed.devices["fcu1"].values["airflow_m3_h"] > 0
    session.set_controls("valve", {"opening_pct": 100})
    assert session.step().devices["fcu1"].values["water_flow_m3_h"] > 0


def test_shared_valve_and_serial_valves_have_physical_effect(water_project):
    insert_valve(water_project, "pump", "main", "main_valve")
    insert_valve(water_project, "s1", "fcu1", "v1")
    insert_valve(water_project, "v1", "fcu1", "v2")
    full = run_project(water_project).snapshot
    half = run_project(water_project, {"main_valve": {"opening_pct": 50}}).snapshot
    assert 0 < half.chilled_flow_m3_h < full.chilled_flow_m3_h
    for valve_id in ("v1", "v2"):
        closed = run_project(water_project, {valve_id: {"opening_pct": 0}}).snapshot
        assert closed.devices["fcu1"].values["water_flow_m3_h"] == 0
        assert closed.devices["fcu2"].values["water_flow_m3_h"] > 0
    closed = run_project(water_project, {"main_valve": {"opening_pct": 0}}).snapshot
    assert closed.chilled_flow_m3_h == 0


def test_missing_return_cannot_circulate(water_project):
    water_project.connections = [
        e for e in water_project.connections if (e.from_id, e.to_id) != ("fcu1", "rtn1")
    ]
    session = run_project(water_project)
    assert session.snapshot.devices["fcu1"].values["water_flow_m3_h"] == 0
    assert session.snapshot.devices["fcu2"].values["water_flow_m3_h"] > 0
    assert any(i.target == "fcu1" for i in session.snapshot.network.issues)


def test_water_valve_cannot_be_used_as_an_air_damper():
    project = SystemEvaluateRequest.model_validate(SAMPLES["multi_zone_all_air"])
    insert_valve(project, "supply_main", "supply_office")
    snapshot = run_project(project).snapshot
    assert snapshot.rooms["zone_office"].airflow_m3_h == 0
    assert any(
        i.code == "SIM_CONNECTION_MEDIUM_MISMATCH" and i.target == "valve"
        for i in snapshot.network.issues
    )


def test_bypass_label_alone_does_not_change_the_actual_connections(water_project):
    baseline = run_project(water_project).snapshot
    water_project.pipes[0].pipe_type = "bypass"
    snapshot = run_project(water_project).snapshot
    assert snapshot.chilled_flow_m3_h == baseline.chilled_flow_m3_h
    assert snapshot.devices == baseline.devices
    assert all(c.model_status == "modeled" for c in snapshot.network.circuits)
    assert not snapshot.network.issues


def test_full_path_resistance_limits_flow(water_project):
    baseline = run_project(water_project).snapshot
    main = next(p for p in water_project.pipes if p.id == "main")
    main.diameter_mm = 10
    main.length_m = 1000
    restricted = run_project(water_project).snapshot
    assert restricted.chilled_flow_m3_h < baseline.chilled_flow_m3_h * 0.1
    assert restricted.cooling_load_kw < baseline.cooling_load_kw


def test_disjoint_circuits_do_not_share_pumps_or_temperatures(water_project):
    other = water_project.model_dump()
    for collection in (other["devices"], other["pipes"], other["rooms"]):
        for item in collection:
            item["id"] = "b_" + item["id"]
    for room in other["rooms"]:
        room["device_ids"] = ["b_" + key for key in room["device_ids"]]
    for edge in other["connections"]:
        edge["from_id"] = "b_" + edge["from_id"]
        edge["to_id"] = "b_" + edge["to_id"]
    merged = water_project.model_dump()
    for key in ("rooms", "devices", "pipes", "connections"):
        merged[key].extend(other[key])
    project = SystemEvaluateRequest.model_validate(merged)
    baseline = run_project(project).snapshot
    snapshot = run_project(
        project, {"b_pump": {"enabled": 0}, "b_source": {"enabled": 0}}
    ).snapshot
    assert snapshot.devices["b_fcu1"].values["water_flow_m3_h"] == 0
    assert snapshot.devices["b_fcu2"].values["cooling_kw"] == 0
    assert snapshot.devices["fcu1"].values == baseline.devices["fcu1"].values


def test_cooling_water_shutdown_interlocks_only_its_water_cooled_source():
    project = SystemEvaluateRequest.model_validate(SAMPLES["four_floor_fcu_fresh_air"])
    baseline = run_project(project).snapshot
    assert baseline.devices["chiller_b1"].values["cooling_kw"] > 0
    snapshot = run_project(project, {"cw_pump_b1": {"enabled": 0}}).snapshot
    assert snapshot.cooling_flow_m3_h == 0
    assert snapshot.devices["chiller_b1"].values["cooling_kw"] == 0
    assert snapshot.devices["chiller_b1"].values["power_kw"] == 0
    assert any(i.code == "SIM_CONDENSER_FLOW_MISSING" for i in snapshot.network.issues)


def test_air_connections_are_required_for_ducted_supply():
    project = SystemEvaluateRequest.model_validate(SAMPLES["ducted_fresh_air"])
    baseline = run_project(project).snapshot
    assert baseline.devices["fau_b1"].values["airflow_m3_h"] > 0
    project.connections = [e for e in project.connections if e.system != "air"]
    broken = run_project(project).snapshot
    assert broken.devices["fau_b1"].values["airflow_m3_h"] == 0
    assert broken.devices["fau_b1"].values["cooling_kw"] == 0
    assert broken.devices["fcu_f1"].values["airflow_m3_h"] == 0


def test_local_fcu_is_not_converted_to_a_ducted_unit_by_room_outlet_bindings(
    water_project,
):
    water_project.devices.append(DeviceInput(id="fresh_outlet", type="送风口"))
    water_project.rooms[0].supply_air_outlet_ids = ["fresh_outlet"]
    snapshot = run_project(water_project).snapshot
    assert snapshot.devices["fcu1"].values["airflow_m3_h"] > 0
    assert snapshot.devices["fcu1"].values["cooling_kw"] > 0


def test_air_damper_stops_its_room_and_preserves_other_branches():
    project = SystemEvaluateRequest.model_validate(SAMPLES["multi_zone_all_air"])
    insert_valve(project, "supply_main", "supply_office")
    valve = next(device for device in project.devices if device.id == "valve")
    valve.type, valve.name = "风阀", None
    baseline = run_project(project).snapshot
    closed = run_project(project, {"valve": {"opening_pct": 0}}).snapshot
    assert baseline.rooms["zone_office"].airflow_m3_h > 0
    assert closed.rooms["zone_office"].airflow_m3_h == 0
    assert closed.rooms["zone_meeting"].airflow_m3_h > 0
    assert closed.devices["ahu_f1"].values["airflow_m3_h"] == pytest.approx(
        sum(room.airflow_m3_h for room in closed.rooms.values())
    )
    ducts = {node.node_id: node for node in closed.network.ducts}
    assert ducts["supply_office"].flow_m3_h == 0
    assert ducts["return_office"].flow_m3_h == 0


def test_water_circuit_energy_balance_uses_its_own_flow_and_return(water_project):
    from app.simcore.hydronics import WATER_KW_PER_M3H_K

    snapshot = run_project(water_project).snapshot
    water = next(c for c in snapshot.network.circuits if c.system == "chilled_water")
    assert water.flow_m3_h == pytest.approx(
        sum(d.values.get("water_flow_m3_h", 0.0) for d in snapshot.devices.values())
    )
    assert snapshot.cooling_load_kw == pytest.approx(
        WATER_KW_PER_M3H_K
        * water.flow_m3_h
        * (water.return_temp_c - water.supply_temp_c)
    )
    assert snapshot.cooling_load_kw == pytest.approx(
        snapshot.devices["source"].values["cooling_kw"]
    )


def test_steady_drift_checks_independent_circuits_instead_of_their_mean(water_project):
    from app.schemas import RealtimeNetworkResult, RuntimeCircuitState
    from app.simcore.steady import _temperature_drift

    snapshot = run_project(water_project).snapshot
    before = replace(
        snapshot,
        network=RealtimeNetworkResult(
            circuits=[
                RuntimeCircuitState(
                    circuit_id="a",
                    system="chilled_water",
                    flow_m3_h=1,
                    supply_temp_c=8,
                    return_temp_c=10,
                ),
                RuntimeCircuitState(
                    circuit_id="b",
                    system="chilled_water",
                    flow_m3_h=1,
                    supply_temp_c=10,
                    return_temp_c=12,
                ),
            ]
        ),
    )
    after = replace(
        snapshot,
        network=RealtimeNetworkResult(
            circuits=[
                RuntimeCircuitState(
                    circuit_id="a",
                    system="chilled_water",
                    flow_m3_h=1,
                    supply_temp_c=9,
                    return_temp_c=11,
                ),
                RuntimeCircuitState(
                    circuit_id="b",
                    system="chilled_water",
                    flow_m3_h=1,
                    supply_temp_c=9,
                    return_temp_c=11,
                ),
            ]
        ),
    )
    assert _temperature_drift(before, after) == pytest.approx(1)


@pytest.mark.parametrize(
    "mode,with_chiller",
    [(SimulationMode.COOLING, False), (SimulationMode.HEATING, False),
     (SimulationMode.COOLING, True)],
)
@pytest.mark.parametrize("second_source_enabled", [True, False])
def test_parallel_sources_conserve_individual_and_mixed_water_energy(
    water_project, mode, with_chiller, second_source_enabled,
):
    from app.simcore.hydronics import WATER_KW_PER_M3H_K
    from app.simcore.steady import solve_steady

    system = "hot_water" if mode is SimulationMode.HEATING else "chilled_water"
    for pipe in water_project.pipes:
        pipe.system = system
    for edge in water_project.connections:
        edge.system = system
    water_project.devices.append(DeviceInput(
        id="source_b", type="空气源热泵", name="SHAF-80H", quantity=2,
    ))
    water_project.connections.extend([
        ConnectionInput(from_id="source_b", to_id="pump", system=system),
        ConnectionInput(from_id="return", to_id="source_b", system=system),
    ])
    if with_chiller:
        sample = SAMPLES["four_floor_fcu_fresh_air"]
        chiller = next(d for d in sample["devices"] if d["id"] == "chiller_b1")
        tower = next(d for d in sample["devices"] if d["type"] == "冷却塔")
        water_project.devices[0] = DeviceInput(**{**chiller, "id": "source"})
        water_project.devices.extend([
            DeviceInput(id="cw_pump", type="循环泵", name="TPE3 65-150"),
            DeviceInput(**{**tower, "id": "tower"}),
        ])
        water_project.connections.extend(
            ConnectionInput(from_id=a, to_id=b, system="cooling_water")
            for a, b in [("source", "cw_pump"), ("cw_pump", "tower"), ("tower", "source")]
        )
    controls = {"source_b": {"enabled": float(second_source_enabled)}}
    initial = run_project(water_project, mode=mode, ticks=0)
    # A 10 kW source alongside a larger one gives unequal Q/V. At this small
    # test flow two large catalogue sources would both hit the same 20 K cap.
    layout = replace(initial.layout, devices=tuple(
        replace(node, rated=replace(node.rated, capacity_kw=10.0))
        if node.instance_id == "source" else node
        for node in initial.layout.devices
    ))
    session = SimulationSession(layout, initial.config)
    for key, value in controls.items():
        session.set_controls(key, value)
    for _ in range(120):
        session.step()
    snapshot = session.snapshot
    circuit = next(c for c in snapshot.network.circuits if c.system == system)
    values = [snapshot.devices[key].values for key in ("source", "source_b")]
    load_key = "heating_kw" if mode is SimulationMode.HEATING else "cooling_kw"
    sign = 1.0 if mode is SimulationMode.HEATING else -1.0
    projected = {n.node_id: n for n in snapshot.network.devices if n.system == system}
    for key, reading in zip(("source", "source_b"), values, strict=True):
        assert reading["flow_m3_h"] > 0
        assert reading["return_temp_c"] == pytest.approx(circuit.return_temp_c)
        assert reading[load_key] == pytest.approx(
            sign * WATER_KW_PER_M3H_K * reading["flow_m3_h"]
            * (reading["supply_temp_c"] - reading["return_temp_c"]), abs=1e-9,
        )
        assert projected[key].supply_temp_c == pytest.approx(reading["supply_temp_c"])
        assert projected[key].return_temp_c == pytest.approx(reading["return_temp_c"])
    total_flow = sum(v["flow_m3_h"] for v in values)
    assert total_flow == pytest.approx(circuit.flow_m3_h)
    mixed = sum(v["supply_temp_c"] * v["flow_m3_h"] for v in values) / total_flow
    assert mixed == pytest.approx(circuit.supply_temp_c)
    assert sum(v[load_key] for v in values) == pytest.approx(
        snapshot.heating_load_kw if mode is SimulationMode.HEATING else snapshot.cooling_load_kw
    )
    assert abs(values[0]["supply_temp_c"] - values[1]["supply_temp_c"]) > 0.01
    if not second_source_enabled:
        assert values[1][load_key] == 0.0
        assert values[1]["supply_temp_c"] == values[1]["return_temp_c"]

    steady = solve_steady(
        session.layout, session.config, max_ticks=120, initial_controls=controls,
    )
    assert steady.ticks == session.snapshot.tick
    assert steady.snapshot.devices == snapshot.devices
    assert steady.snapshot.network == snapshot.network


def test_network_pipe_and_device_flows_are_the_same_result(water_project):
    insert_valve(water_project, "s1", "fcu1")
    snapshot = run_project(water_project).snapshot
    pipes = {p.node_id: p for p in snapshot.network.pipes}
    branch = snapshot.devices["fcu1"].values["water_flow_m3_h"]
    assert pipes["s1"].flow_m3_h == pytest.approx(branch)
    assert pipes["rtn1"].flow_m3_h == pytest.approx(branch)
    assert pipes["main"].flow_m3_h == pytest.approx(snapshot.chilled_flow_m3_h)
    assert pipes["return"].flow_m3_h == pytest.approx(snapshot.chilled_flow_m3_h)
    assert snapshot.devices["valve"].values["flow_m3_h"] == pytest.approx(branch)


def test_order_and_steady_state_use_the_same_connected_model(water_project):
    from app.simcore.steady import solve_steady

    reordered = deepcopy(water_project)
    for items in (
        reordered.rooms,
        reordered.devices,
        reordered.pipes,
        reordered.connections,
    ):
        items.reverse()
    baseline = run_project(water_project).snapshot
    shuffled = run_project(reordered).snapshot
    assert baseline.chilled_flow_m3_h == pytest.approx(shuffled.chilled_flow_m3_h)
    assert baseline.cooling_load_kw == pytest.approx(shuffled.cooling_load_kw)
    session = run_project(water_project, ticks=1)
    steady = solve_steady(session.layout, session.config, max_ticks=120)
    transient = run_project(water_project, ticks=steady.ticks).snapshot
    assert steady.snapshot.chilled_flow_m3_h == pytest.approx(
        transient.chilled_flow_m3_h
    )
    assert steady.snapshot.cooling_load_kw == pytest.approx(transient.cooling_load_kw)
    assert replace(steady.snapshot, network=None) == replace(transient, network=None)


def _use_water_medium(project, system):
    for pipe in project.pipes:
        pipe.system = system
    for edge in project.connections:
        edge.system = system
    if system == "cooling_water":
        project.rooms = []
        for device in project.devices:
            if device.id == "source":
                device.type, device.name = "冷源机组", "SCWE-100H+B"
            elif device.id.startswith("fcu"):
                device.type, device.name = "冷却塔", "S15E-1285-06JN"
    return SimulationMode.HEATING if system == "hot_water" else SimulationMode.COOLING


@pytest.mark.parametrize("system", ["chilled_water", "hot_water", "cooling_water"])
@pytest.mark.parametrize("kind", ["unknown", "branch_supply", "direct", "local"])
def test_actual_bypass_splits_flow_and_mixes_return_for_every_water_medium(water_project, system, kind):
    from app.simcore.hydronics import WATER_KW_PER_M3H_K
    mode = _use_water_medium(water_project, system)
    baseline = run_project(water_project, mode=mode, ticks=2).snapshot
    assert all(c.model_status == "modeled" and c.flow_m3_h > 0 for c in baseline.network.circuits)
    if kind == "direct":
        links = [("main", "return")]
    else:
        water_project.pipes.append(PipeInput(
            id="extra", system=system, pipe_type="branch_supply" if kind == "branch_supply" else "unknown",
            diameter_mm=65, length_m=10,
        ))
        links = [("s1", "extra"), ("extra", "rtn1")] if kind == "local" else [("main", "extra"), ("extra", "return")]
    water_project.connections.extend(ConnectionInput(from_id=a, to_id=b, system=system) for a, b in links)
    snapshot = run_project(water_project, mode=mode, ticks=2).snapshot
    circuit = snapshot.network.circuits[0]
    assert circuit.model_status == "modeled"
    nodes = {n.node_id: n for n in snapshot.network.devices}
    load_flow = nodes["fcu1"].flow_m3_h + nodes["fcu2"].flow_m3_h
    assert circuit.flow_m3_h > load_flow > 0
    assert snapshot.devices["pump"].values["flow_m3_h"] == pytest.approx(circuit.flow_m3_h)
    if kind != "direct":
        bypass_flow = next(n.flow_m3_h for n in snapshot.network.pipes if n.node_id == "extra")
        assert circuit.flow_m3_h == pytest.approx(load_flow + bypass_flow)
    if system != "cooling_water":
        heating = mode is SimulationMode.HEATING
        load = snapshot.heating_load_kw if heating else snapshot.cooling_load_kw
        delta = (circuit.supply_temp_c - circuit.return_temp_c) if heating else (circuit.return_temp_c - circuit.supply_temp_c)
        assert load == pytest.approx(WATER_KW_PER_M3H_K * circuit.flow_m3_h * delta)
    assert not snapshot.network.issues


@pytest.mark.parametrize("system", ["chilled_water", "hot_water", "cooling_water"])
@pytest.mark.parametrize("position", ["supply", "return", "mixed_parallel"])
def test_series_pumps_carry_the_same_flow_without_double_counting(water_project, system, position):
    mode = _use_water_medium(water_project, system)
    pump = next(device for device in water_project.devices if device.id == "pump")
    water_project.devices.append(pump.model_copy(update={"id": "pump2"}))
    replaced = ("return", "source") if position == "return" else ("pump", "main")
    water_project.connections = [e for e in water_project.connections if (e.from_id, e.to_id) != replaced]
    links = [(replaced[0], "pump2"), ("pump2", replaced[1])]
    if position == "mixed_parallel":
        water_project.devices.append(pump.model_copy(update={"id": "pump3"}))
        links.extend([("source", "pump3"), ("pump3", "main")])
    water_project.connections.extend(ConnectionInput(from_id=a, to_id=b, system=system) for a, b in links)
    snapshot = run_project(water_project, mode=mode, ticks=2).snapshot
    circuit = snapshot.network.circuits[0]
    q1 = snapshot.devices["pump"].values["flow_m3_h"]
    q2 = snapshot.devices["pump2"].values["flow_m3_h"]
    q3 = snapshot.devices["pump3"].values["flow_m3_h"] if position == "mixed_parallel" else 0.0
    assert circuit.model_status == "modeled" and circuit.flow_m3_h > 0
    assert q1 == pytest.approx(q2)
    assert circuit.flow_m3_h == pytest.approx(q1 + q3)
    assert getattr(snapshot, {"chilled_water": "chilled_flow_m3_h", "hot_water": "hot_water_flow_m3_h", "cooling_water": "cooling_flow_m3_h"}[system]) == pytest.approx(q1 + q3)
    assert not snapshot.network.issues
    stopped = run_project(water_project, {"pump2": {"enabled": 0}}, mode=mode, ticks=2).snapshot
    assert stopped.devices["pump"].values["flow_m3_h"] == 0
    assert stopped.devices["pump2"].values["flow_m3_h"] == 0
    assert (stopped.network.circuits[0].flow_m3_h > 0) == (position == "mixed_parallel")


def test_parallel_pumps_still_share_the_flow(water_project):
    pump = next(device for device in water_project.devices if device.id == "pump")
    water_project.devices.append(pump.model_copy(update={"id": "pump2"}))
    water_project.connections.extend([
        ConnectionInput(from_id="source", to_id="pump2", system="chilled_water"),
        ConnectionInput(from_id="pump2", to_id="main", system="chilled_water"),
    ])
    snapshot = run_project(water_project, ticks=2).snapshot
    assert snapshot.chilled_flow_m3_h == pytest.approx(3.784)
    assert snapshot.devices["pump"].values["flow_m3_h"] == pytest.approx(1.892)
    assert snapshot.devices["pump2"].values["flow_m3_h"] == pytest.approx(1.892)
    assert not snapshot.network.issues


def test_cross_link_computes_all_alternative_routes_and_terminals(water_project):
    water_project.connections.append(ConnectionInput(from_id="s1", to_id="s2", system="chilled_water"))
    snapshot = run_project(water_project, ticks=2).snapshot
    assert snapshot.chilled_flow_m3_h == pytest.approx(3.784)
    q1 = snapshot.devices["fcu1"].values["water_flow_m3_h"]
    q2 = snapshot.devices["fcu2"].values["water_flow_m3_h"]
    assert q1 == pytest.approx(1.892) and q2 == pytest.approx(1.892)
    assert next(p.flow_m3_h for p in snapshot.network.pipes if p.node_id == "s1") > q1
    assert all(c.model_status == "modeled" for c in snapshot.network.circuits)
    assert not snapshot.network.issues


def test_closing_a_bypass_restores_terminal_flow_without_breaking_energy_balance(water_project):
    from app.simcore.hydronics import WATER_KW_PER_M3H_K

    next(p for p in water_project.pipes if p.id == "main").diameter_mm = 15
    water_project.connections.append(ConnectionInput(from_id="main", to_id="return", system="chilled_water"))
    insert_valve(water_project, "main", "return", "bypass_valve")
    opened = run_project(water_project, ticks=30).snapshot
    closed = run_project(water_project, {"bypass_valve": {"opening_pct": 0}}, ticks=30).snapshot
    assert opened.devices["bypass_valve"].values["flow_m3_h"] > 0
    assert closed.devices["bypass_valve"].values["flow_m3_h"] == 0
    for key in ("fcu1", "fcu2"):
        assert closed.devices[key].values["water_flow_m3_h"] > opened.devices[key].values["water_flow_m3_h"]
    for snap in (opened, closed):
        circuit = snap.network.circuits[0]
        assert snap.cooling_load_kw == pytest.approx(
            WATER_KW_PER_M3H_K * circuit.flow_m3_h * (circuit.return_temp_c - circuit.supply_temp_c)
        )


def test_bypass_only_flow_still_requires_water_temperature_to_settle(water_project):
    from app.simcore.steady import solve_steady

    water_project.rooms = []
    water_project.connections.append(ConnectionInput(from_id="main", to_id="return", system="chilled_water"))
    session = run_project(water_project, ticks=0)
    early = solve_steady(session.layout, session.config, max_ticks=1)
    assert early.snapshot.chilled_flow_m3_h > 0
    assert early.snapshot.cooling_load_kw == 0
    assert not early.converged
    settled = solve_steady(session.layout, session.config, max_ticks=200)
    assert settled.converged and settled.ticks > 1


def test_condenser_bypass_cannot_replace_cooling_tower_heat_rejection():
    project = SystemEvaluateRequest.model_validate(SAMPLES["four_floor_fcu_fresh_air"])
    project.connections.append(ConnectionInput(
        from_id="cw_pump_b1", to_id="chiller_b1", system="cooling_water",
    ))
    baseline = run_project(project).snapshot
    assert baseline.devices["chiller_b1"].values["cooling_kw"] > 0
    stopped = run_project(project, {"tower_roof": {"enabled": 0}}).snapshot
    assert stopped.cooling_flow_m3_h > 0  # Water still circulates through the bypass.
    assert stopped.devices["chiller_b1"].values["cooling_kw"] == 0
    assert stopped.devices["chiller_b1"].values["power_kw"] == 0
    assert any(i.code == "SIM_CONDENSER_FLOW_MISSING" for i in stopped.network.issues)


@pytest.mark.parametrize("limit_name", ["MAX_COMPLEX_WATER_ROUTES", "MAX_CYCLE_SEARCH_STEPS"])
def test_complex_route_limits_reject_the_whole_component_not_a_partial_solution(water_project, monkeypatch, limit_name):
    from app.simcore import runtime_network

    monkeypatch.setattr(runtime_network, limit_name, 1)
    water_project.connections.append(ConnectionInput(from_id="main", to_id="return", system="chilled_water"))
    snapshot = run_project(water_project, ticks=1).snapshot
    assert snapshot.chilled_flow_m3_h == 0
    assert all(c.model_status == "unsupported" for c in snapshot.network.circuits)
    assert any("截断通路" in issue.message for issue in snapshot.network.issues)


@pytest.mark.parametrize("kind", ["bypass", "series", "ring"])
def test_complex_routes_are_order_independent_and_share_the_steady_core(water_project, kind):
    from app.simcore.steady import solve_steady

    if kind == "bypass":
        links = [("main", "return")]
    elif kind == "ring":
        links = [("s1", "s2")]
    else:
        pump = next(d for d in water_project.devices if d.id == "pump")
        water_project.devices.append(pump.model_copy(update={"id": "pump2"}))
        water_project.connections = [e for e in water_project.connections if (e.from_id, e.to_id) != ("pump", "main")]
        links = [("pump", "pump2"), ("pump2", "main")]
    water_project.connections.extend(ConnectionInput(from_id=a, to_id=b, system="chilled_water") for a, b in links)
    session = run_project(water_project, ticks=120)
    snapshot = session.snapshot
    steady = solve_steady(session.layout, session.config, max_ticks=120)
    assert steady.snapshot == snapshot
    shuffled = deepcopy(water_project)
    for items in (shuffled.devices, shuffled.rooms, shuffled.pipes, shuffled.connections):
        items.reverse()
    reordered = run_project(shuffled, ticks=120).snapshot
    assert reordered.devices == snapshot.devices
    assert reordered.network == snapshot.network


@pytest.mark.parametrize("with_pump", [False, True])
def test_a_dead_end_does_not_disable_complete_branches(water_project, with_pump):
    baseline = run_project(water_project, ticks=2).snapshot
    water_project.pipes.append(PipeInput(id="extra", system="chilled_water", diameter_mm=65))
    links = [("main", "extra")]
    if with_pump:
        pump = next(device for device in water_project.devices if device.id == "pump")
        water_project.devices.append(pump.model_copy(update={"id": "unused_pump"}))
        links = [("main", "unused_pump"), ("unused_pump", "extra")]
    water_project.connections.extend(ConnectionInput(from_id=a, to_id=b, system="chilled_water") for a, b in links)
    snapshot = run_project(water_project, ticks=2).snapshot
    assert snapshot.chilled_flow_m3_h == baseline.chilled_flow_m3_h
    assert all(snapshot.devices[key].values == reading.values for key, reading in baseline.devices.items())
    assert next(node for node in snapshot.network.pipes if node.node_id == "extra").model_status == "disconnected"
    assert not any(issue.code == "SIM_NETWORK_UNSUPPORTED" for issue in snapshot.network.issues)


def test_unsupported_loop_does_not_change_an_independent_loop(water_project):
    other = water_project.model_dump()
    for collection in (other["rooms"], other["devices"], other["pipes"]):
        for item in collection:
            item["id"] = "b_" + item["id"]
    for room in other["rooms"]:
        room["device_ids"] = ["b_" + key for key in room["device_ids"]]
    for edge in other["connections"]:
        edge["from_id"], edge["to_id"] = "b_" + edge["from_id"], "b_" + edge["to_id"]
    merged = water_project.model_dump()
    for key in ("rooms", "devices", "pipes", "connections"):
        merged[key].extend(other[key])
    project = SystemEvaluateRequest.model_validate(merged)
    baseline = run_project(project, ticks=2).snapshot
    insert_valve(project, "main", "s1")
    next(d for d in project.devices if d.id == "valve").quantity = 2
    snapshot = run_project(project, ticks=2).snapshot
    for key, reading in baseline.devices.items():
        if key.startswith("b_"):
            assert snapshot.devices[key].values == reading.values
    assert next(c for c in snapshot.network.circuits if "source" in c.source_ids).model_status == "unsupported"
    assert next(c for c in snapshot.network.circuits if "b_source" in c.source_ids).model_status == "modeled"


def test_a_closed_series_source_loop_is_not_misreported_as_open(water_project):
    source = next(device for device in water_project.devices if device.id == "source")
    water_project.devices.append(source.model_copy(update={"id": "source2"}))
    water_project.connections = [e for e in water_project.connections if (e.from_id, e.to_id) != ("return", "source")]
    water_project.connections.extend([
        ConnectionInput(from_id="return", to_id="source2", system="chilled_water"),
        ConnectionInput(from_id="source2", to_id="source", system="chilled_water"),
    ])
    snapshot = run_project(water_project, ticks=2).snapshot
    assert snapshot.chilled_flow_m3_h == 0
    assert all(c.model_status == "unsupported" for c in snapshot.network.circuits)
    assert any(issue.code == "SIM_NETWORK_UNSUPPORTED" for issue in snapshot.network.issues)
    assert not any(issue.code == "SIM_WATER_PATH_INCOMPLETE" for issue in snapshot.network.issues)


def test_unsupported_topology_reaches_steady_rest_and_websocket(water_project):
    from fastapi.testclient import TestClient

    from app.main import app

    insert_valve(water_project, "main", "s1")
    next(d for d in water_project.devices if d.id == "valve").quantity = 2
    with TestClient(app) as client:
        steady = client.post("/api/v1/simulation/steady-state", json=water_project.model_dump())
        assert steady.status_code == 200
        assert any(issue["code"] == "SIM_NETWORK_UNSUPPORTED" for issue in steady.json()["issues"])
        saved = client.post("/api/v1/projects", json={"name": "unsupported-loop", "project": water_project.model_dump()})
        project_id = saved.json()["project_id"]
        try:
            started = client.post(f"/api/v1/realtime/simulate/start/{project_id}", json={"interval_seconds": 3600})
            assert started.status_code == 200
            state = client.get(f"/api/v1/realtime/state/{project_id}").json()
            assert all(c["model_status"] == "unsupported" for c in state["network"]["circuits"])
            pipes = client.get(f"/api/v1/realtime/pipes/{project_id}").json()["pipes"]
            assert all(p["model_status"] == "unsupported" for p in pipes)
            with client.websocket_connect(f"/ws/realtime/{project_id}") as socket:
                assert socket.receive_json()["data"] == state
        finally:
            client.delete(f"/api/v1/projects/{project_id}")
