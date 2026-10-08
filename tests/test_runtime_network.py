"""Connection-driven runtime regressions; all projects are isolated fixtures.

These cases exercise physical consequences of edits, not merely diagnostics or
the presence of response fields. Closed water circuits and explicit air outlets
make the intended boundary conditions independent of catalogue defaults.
"""

from copy import deepcopy
from dataclasses import replace

import pytest

from app.samples import SAMPLES
from app.schemas import ConnectionInput, DeviceInput, SystemEvaluateRequest
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


def test_bypass_is_reported_as_unsupported_without_inventing_flow(water_project):
    water_project.pipes[0].pipe_type = "bypass"
    snapshot = run_project(water_project).snapshot
    assert snapshot.chilled_flow_m3_h == 0
    assert any(c.model_status == "unsupported" for c in snapshot.network.circuits)
    assert any(i.code == "SIM_NETWORK_UNSUPPORTED" for i in snapshot.network.issues)


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
