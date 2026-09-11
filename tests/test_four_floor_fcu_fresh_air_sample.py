from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.samples import FOUR_FLOOR_FCU_FRESH_AIR_SAMPLE
from app.schemas import SystemEvaluateRequest
from app.simcore.hydronics import WATER_KW_PER_M3H_K
from app.simcore.project import build_project_layout
from app.simcore.session import SimulationConfig, SimulationMode, SimulationSession
from app.steady_solver.port_graph import build_port_graph


client = TestClient(app)
SAMPLE_PATH = "/api/v1/project/sample/four_floor_fcu_fresh_air"


def _sample() -> dict:
    return deepcopy(FOUR_FLOOR_FCU_FRESH_AIR_SAMPLE)


def _without_device(device_id: str) -> dict:
    payload = _sample()
    payload["devices"] = [device for device in payload["devices"] if device["id"] != device_id]
    payload["connections"] = [
        connection
        for connection in payload["connections"]
        if device_id not in {connection["from_id"], connection["to_id"]}
    ]
    return payload


def _validation_codes(payload: dict) -> set[str]:
    response = client.post("/api/v1/project/validate", json=payload)
    assert response.status_code == 200
    return {issue["code"] for issue in response.json()["issues"]}


def _evaluation(payload: dict) -> dict:
    response = client.post("/api/v1/system/evaluate", json=payload)
    assert response.status_code == 200
    return response.json()


def test_sample_endpoint_returns_complete_four_floor_annual_system():
    response = client.get(SAMPLE_PATH)

    assert response.status_code == 200
    payload = response.json()
    assert [floor["id"] for floor in payload["floors"]] == ["B1F", "F1", "F2", "F3"]
    assert {room["floor_id"] for room in payload["rooms"]} == {"F1", "F2", "F3"}

    devices = {device["id"]: device for device in payload["devices"]}
    assert devices["chiller_b1"]["floor_id"] == "B1F"
    assert devices["ashp_roof"]["location_type"] == "roof"
    assert devices["tower_roof"]["location_type"] == "roof"
    # 新风由接冷冻水的组合式空调机组承担；一体化直膨新风机组在仿真里无冷源，
    # 已从样例移除（见 samples.py 注释）。
    assert {devices[f"ahu_{floor}"]["type"] for floor in ("f1", "f2", "f3")} == {"组合式空调机组"}

    request = SystemEvaluateRequest(**payload)
    assert len(request.floors) == 4
    assert {pipe.system for pipe in request.pipes} == {"chilled_water", "cooling_water", "hot_water", "air"}


def test_complete_sample_validates_and_evaluates_without_errors():
    payload = client.get(SAMPLE_PATH).json()
    validation = client.post("/api/v1/project/validate", json=payload)
    evaluation = _evaluation(payload)

    assert validation.status_code == 200
    assert validation.json()["valid"] is True
    assert validation.json()["issues"] == []
    assert not [issue for issue in evaluation["issues"] if issue["level"] == "error"]
    assert evaluation["load"]["adjusted_cooling_kw"] > 0
    assert evaluation["load"]["adjusted_heating_kw"] > 0

    flows = evaluation["hydraulic_network"]["total_flow_by_system_m3_h"]
    assert all(flows[system] > 0 for system in ("chilled_water", "cooling_water", "hot_water"))
    for floor_id in ("F1", "F2", "F3"):
        water = evaluation["hydraulic_network"]["floor_flow_summary"][floor_id]
        assert water["chilled_water"] > 0
        assert water["hot_water"] > 0
        assert evaluation["air_network"]["floor_airflow_summary"][floor_id]["air"] > 0

    assert {riser["system"] for riser in evaluation["topology"]["vertical_network"]["risers"]} == {
        "air",
        "chilled_water",
        "cooling_water",
        "hot_water",
    }
    assert all(item["converged"] for item in evaluation["steady_network"]["media"].values())
    equipment = evaluation["steady_network"]["equipment"]
    assert equipment["cooling_balance_residual_kw"] == 0
    assert equipment["heating_balance_residual_kw"] == 0


def test_port_graph_exposes_hot_water_ports_for_heat_source_and_connected_fcus():
    graph = build_port_graph(SystemEvaluateRequest(**_sample()))

    assert [port.medium for port in graph.device_ports["ashp_roof"]] == ["hot_water", "hot_water"]
    for device_id in ("fcu_f1", "fcu_f2", "fcu_f3"):
        media = [port.medium for port in graph.device_ports[device_id]]
        assert media.count("chilled_water") == 2
        assert media.count("hot_water") == 2
        assert media.count("air") == 2


def test_simcore_separates_chilled_cooling_and_hot_water_pumps():
    layout = build_project_layout(SystemEvaluateRequest(**_sample())).layout
    loops = {node.instance_id: node.loop for node in layout.devices}

    assert loops["chw_pump_b1"] == "chilled"
    assert loops["cw_pump_b1"] == "cooling"
    assert loops["hw_pump_b1"] == "hot"


def test_simcore_builds_stable_fcu_hydronic_branches_from_direct_pipes():
    payload = _sample()
    layout = build_project_layout(SystemEvaluateRequest(**payload)).layout
    reversed_payload = _sample()
    reversed_payload["pipes"].reverse()
    reversed_payload["connections"].reverse()
    reversed_layout = build_project_layout(
        SystemEvaluateRequest(**reversed_payload)
    ).layout

    branches = {
        (branch.loop, branch.terminal_instance_id): branch.resistance_index
        for branch in layout.hydronic_branches
    }
    assert layout.modeled_water_loops == frozenset({"chilled", "hot"})
    # 冷冻侧：风机盘管 + 组合式新风机组；热水侧：仅风机盘管（新风机组不接热水）
    assert set(branches) == {
        ("chilled", f"{terminal}_{floor}")
        for terminal in ("fcu", "ahu")
        for floor in ("f1", "f2", "f3")
    } | {("hot", f"fcu_{floor}") for floor in ("f1", "f2", "f3")}
    assert branches[("chilled", "fcu_f2")] > branches[("chilled", "fcu_f3")]
    assert branches[("chilled", "fcu_f3")] > branches[("chilled", "fcu_f1")]
    assert layout.hydronic_branches == reversed_layout.hydronic_branches
    # 新风机组必须真的接入冷冻水，否则又会退化成纯风机
    assert any(
        branch.terminal_instance_id.startswith("ahu_")
        for branch in layout.hydronic_branches
        if branch.loop == "chilled"
    )


def test_four_floor_cooling_uses_one_conserved_topology_aware_flow():
    layout = build_project_layout(SystemEvaluateRequest(**_sample())).layout
    session = SimulationSession(
        layout,
        SimulationConfig(
            mode=SimulationMode.COOLING,
            initial_room_temp_c=29.0,
            target_temp_c=26.0,
            automatic_control=False,
        ),
    )

    snapshot = session.step()
    # 冷冻水环路末端 = 风机盘管 + 组合式新风机组
    terminal_ids = [
        f"{terminal}_{floor}"
        for terminal in ("fcu", "ahu")
        for floor in ("f1", "f2", "f3")
    ]
    terminal_flow = sum(
        snapshot.devices[terminal_id].values["water_flow_m3_h"]
        for terminal_id in terminal_ids
    )

    assert snapshot.chilled_flow_m3_h == pytest.approx(terminal_flow, abs=0.001)
    assert snapshot.devices["chw_pump_b1"].values["flow_m3_h"] == pytest.approx(
        terminal_flow, abs=0.001
    )
    # 制冷工况下热泵四通阀换向参与冷源池（教学口径），冷量与水量按容量份额
    # 在冷机与热泵之间分摊。守恒发生在"冷源池"层面：所有冷源水量之和 = 环路水量。
    source_flow = (
        snapshot.devices["chiller_b1"].values["flow_m3_h"]
        + snapshot.devices["ashp_roof"].values["flow_m3_h"]
    )
    assert source_flow == pytest.approx(terminal_flow, abs=0.001)
    assert snapshot.devices["chiller_b1"].values["flow_m3_h"] < terminal_flow
    terminal_cooling = sum(
        snapshot.devices[terminal_id].values["cooling_kw"]
        for terminal_id in terminal_ids
    )
    loop_cooling = (
        WATER_KW_PER_M3H_K
        * snapshot.chilled_flow_m3_h
        * (snapshot.return_water_temp_c - snapshot.supply_water_temp_c)
    )
    assert snapshot.cooling_load_kw == pytest.approx(terminal_cooling)
    assert loop_cooling == pytest.approx(terminal_cooling)
    # 冷量同样在冷源池层面守恒（冷机 + 换向热泵），而非冷机独担。
    source_cooling = (
        snapshot.devices["chiller_b1"].values["cooling_kw"]
        + snapshot.devices["ashp_roof"].values["cooling_kw"]
    )
    assert source_cooling == pytest.approx(terminal_cooling)
    assert snapshot.devices["chiller_b1"].values[
        "return_temp_c"
    ] == pytest.approx(snapshot.return_water_temp_c)
    for floor in ("f1", "f2", "f3"):
        fresh_air = snapshot.devices[f"ahu_{floor}"].values
        # 新风机组接入冷冻水后必须真的产出冷量，否则新风湿负荷无处可去。
        assert fresh_air["water_flow_m3_h"] > 0.0
        assert fresh_air["cooling_kw"] > 0.0
        assert fresh_air["airflow_m3_h"] > 0.0


def test_four_floor_pump_speed_and_valves_change_the_same_closed_loop():
    layout = build_project_layout(SystemEvaluateRequest(**_sample())).layout

    def cooling_at(speed_pct: float):
        session = SimulationSession(
            layout,
            SimulationConfig(
                mode=SimulationMode.COOLING,
                initial_room_temp_c=29.0,
                automatic_control=False,
            ),
        )
        session.set_controls("chw_pump_b1", {"speed_pct": speed_pct})
        return session.step()

    full = cooling_at(100.0)
    low = cooling_at(50.0)
    assert low.chilled_flow_m3_h < full.chilled_flow_m3_h
    assert low.cooling_load_kw < full.cooling_load_kw
    assert (
        low.return_water_temp_c - low.supply_water_temp_c
        > full.return_water_temp_c - full.supply_water_temp_c
    )
    assert (
        low.devices["chw_pump_b1"].values["power_kw"]
        < full.devices["chw_pump_b1"].values["power_kw"]
    )

    session = SimulationSession(
        layout,
        SimulationConfig(
            mode=SimulationMode.COOLING,
            initial_room_temp_c=29.0,
            automatic_control=False,
        ),
    )
    before = session.step()
    session.set_controls("fcu_f1", {"valve_opening_pct": 0.0})
    after_one_closed = session.step()
    assert after_one_closed.devices["fcu_f1"].values["water_flow_m3_h"] == 0.0
    assert after_one_closed.devices["fcu_f1"].values["cooling_kw"] == 0.0
    assert (
        after_one_closed.devices["fcu_f2"].values["water_flow_m3_h"]
        >= before.devices["fcu_f2"].values["water_flow_m3_h"]
    )

    for floor in ("f2", "f3"):
        session.set_controls(f"fcu_{floor}", {"valve_opening_pct": 0.0})
    # 组合式新风机组同样是冷冻水末端，环路仍有流量，不能因 FCU 全关而归零。
    assert session.step().chilled_flow_m3_h > 0.0

    for floor in ("f1", "f2", "f3"):
        session.set_controls(f"ahu_{floor}", {"valve_opening_pct": 0.0})
    all_closed = session.step()
    assert all_closed.chilled_flow_m3_h == 0.0
    assert all_closed.cooling_load_kw == 0.0
    assert all_closed.return_water_temp_c == all_closed.supply_water_temp_c
    assert all_closed.devices["chw_pump_b1"].values["flow_m3_h"] == 0.0
    assert all_closed.devices["chw_pump_b1"].values["power_kw"] == 0.0


def test_four_floor_heating_conserves_hot_flow_and_isolates_other_loops():
    layout = build_project_layout(SystemEvaluateRequest(**_sample())).layout
    session = SimulationSession(
        layout,
        SimulationConfig(
            mode=SimulationMode.HEATING,
            initial_room_temp_c=16.0,
            target_temp_c=22.0,
            outdoor_base_temp_c=5.0,
            automatic_control=False,
        ),
    )

    snapshot = session.step()
    fcu_flow = sum(
        snapshot.devices[f"fcu_{floor}"].values["water_flow_m3_h"]
        for floor in ("f1", "f2", "f3")
    )
    assert snapshot.hot_water_flow_m3_h == pytest.approx(fcu_flow, abs=0.001)
    assert snapshot.devices["hw_pump_b1"].values["flow_m3_h"] == pytest.approx(
        fcu_flow, abs=0.001
    )
    assert snapshot.devices["ashp_roof"].values["flow_m3_h"] == pytest.approx(
        fcu_flow, abs=0.001
    )
    terminal_heating = sum(
        snapshot.devices[f"fcu_{floor}"].values["heating_kw"]
        for floor in ("f1", "f2", "f3")
    )
    loop_heating = (
        WATER_KW_PER_M3H_K
        * snapshot.hot_water_flow_m3_h
        * (snapshot.hot_water_supply_temp_c - snapshot.hot_water_return_temp_c)
    )
    assert snapshot.heating_load_kw == pytest.approx(terminal_heating)
    assert loop_heating == pytest.approx(terminal_heating)
    assert snapshot.devices["ashp_roof"].values["heating_kw"] == pytest.approx(
        terminal_heating
    )
    assert snapshot.devices["ashp_roof"].values[
        "return_temp_c"
    ] == pytest.approx(snapshot.hot_water_return_temp_c)
    assert snapshot.heating_load_kw > 0.0
    assert snapshot.chilled_flow_m3_h == 0.0
    assert snapshot.cooling_flow_m3_h == 0.0


def test_missing_core_devices_and_open_loop_use_existing_diagnostics():
    assert "MISSING_COLD_SOURCE" in _validation_codes(_without_device("chiller_b1"))
    assert "MISSING_COOLING_TOWER" in _validation_codes(_without_device("tower_roof"))

    no_pumps = _sample()
    pump_ids = {"chw_pump_b1", "cw_pump_b1", "hw_pump_b1"}
    no_pumps["devices"] = [device for device in no_pumps["devices"] if device["id"] not in pump_ids]
    no_pumps["connections"] = [
        connection
        for connection in no_pumps["connections"]
        if connection["from_id"] not in pump_ids and connection["to_id"] not in pump_ids
    ]
    assert "MISSING_PUMP" in _validation_codes(no_pumps)

    open_cooling_loop = _sample()
    open_cooling_loop["connections"] = [
        connection
        for connection in open_cooling_loop["connections"]
        if not (
            connection["from_id"] == "tower_roof"
            and connection["to_id"] == "cw_return_riser"
        )
    ]
    assert "OPEN_WATER_LOOP" in _validation_codes(open_cooling_loop)


def test_missing_heat_source_and_fresh_air_connection_remain_observable():
    no_heat_source = _evaluation(_without_device("ashp_roof"))
    assert no_heat_source["steady_network"]["equipment"]["heating_balance_residual_kw"] > 0
    assert "energy_balance_violation" in {
        issue["code"] for issue in no_heat_source["issues"]
    }

    isolated_fresh_air = _sample()
    isolated_fresh_air["connections"] = [
        connection
        for connection in isolated_fresh_air["connections"]
        if "ahu_f2" not in {connection["from_id"], connection["to_id"]}
    ]
    codes = _validation_codes(isolated_fresh_air)
    assert {"ISOLATED_DEVICE", "TERMINAL_NOT_CONNECTED"} <= codes
