from __future__ import annotations

from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from app import realtime
from app.data import project_store
from app.main import app
from app.simcore.session import (
    CHILLER_CAPACITY_LOW_MULTIPLIER,
    SimulationConfig,
)
from app.teaching_scenarios import list_teaching_scenarios

SCENARIO_IDS = [
    "normal_operation",
    "chiller_capacity_low",
    "chilled_water_low_flow",
    "cooling_performance_low",
    "terminal_capacity_low",
]


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(project_store, "PROJECT_DIR", tmp_path / "projects")
    monkeypatch.setattr(realtime, "HISTORY_DIR", tmp_path / "realtime_history")
    for store in _runtime_stores():
        store.clear()
    yield TestClient(app)
    for task in tuple(realtime._sim_tasks.values()):
        task.cancel()
    for store in _runtime_stores():
        store.clear()


def _runtime_stores():
    return (
        realtime._states,
        realtime._updated_at,
        realtime._alarms,
        realtime._summary,
        realtime._subscribers,
        realtime._sim_tasks,
        realtime._sim_configs,
        realtime._sessions,
        realtime._layouts,
        realtime._control_values,
        realtime._last_floor_states,
        realtime._last_control_state,
    )


def _teaching_project() -> dict:
    return {
        "floors": [{"id": "floor-1", "name": "教学层", "level": 1}],
        "rooms": [
            {
                "id": "room-1",
                "floor_id": "floor-1",
                "name": "教学房间",
                "device_ids": ["fcu-1"],
                # 房间内部得热必须与末端阵列能力同一量级，才能让各故障场景都有
                # 可观测效果：
                #   - 得热接近上限会让室温顶在 40℃ 天花板，方向性断言无从体现
                #     （原先 5000 m²/1000 人、后来的 1200 m²/80 人都是这个问题）；
                #   - 得热远低于末端能力则室温被压得过低，冷机长期低负荷，
                #     "冷源能力不足"注入（容量 ×0.55）完全不起作用。
                # 这里取约 77 kW 得热对应 20 台末端（220 kW 名义能力）。
                "area_m2": 2000,
                "people": 133,
                "lighting_w_m2": 12,
                "equipment_w_m2": 18,
            }
        ],
        "devices": [
            {
                "id": "chiller-1",
                "type": "冷源机组",
                "name": "LG340MCF",
                "capacity_kw": 340,
            },
            {
                "id": "pump-chilled",
                "type": "循环泵",
                "name": "EC80",
            },
            {
                "id": "pump-cooling",
                "type": "循环泵",
                "name": "EC80",
            },
            {"id": "tower-1", "type": "冷却塔", "name": "CT-100"},
            {
                "id": "fcu-1",
                "type": "风机盘管",
                "name": "MCW1000V",
                # 容量/水流量覆盖必须与目录风量自洽：ε-NTU 下盘管出力受
                # C_min·ΔT 热力学上限约束，目录风量 2040 m³/h 最多只能携带约
                # 13.7 kW。原先写 300 kW 与风量不匹配，旧模型用钳位掩盖了这个
                # 矛盾（出力被 min(capacity, water_side_limit) 截断，相当于凭空
                # 给出 300 kW 却无对应风量）。这里改用目录真实额定值 11 kW。
                #
                # 台数取 20：
                #   - 冷冻水泵 EC80 额定 20.51 m³/h，降速 45% 约 9.23 m³/h。
                #     末端总水流量须高于该值泵速才会成为约束（单台 1.892 m³/h，
                #     4 台合计 7.57 时流量纹丝不动，观测不到"降泵速"效果），
                #     20 台合计 37.8 m³/h 满足。
                #   - 冷机降容场景把容量乘 0.55（340 -> 187 kW）。末端的换热量
                #     与室温耦合会缩放，187 kW 不是与 220 kW 名义直接比较；
                #     该台数经实测可使降容产生可见的室温上升。
                "floor_id": "floor-1",
                "capacity_kw": 11,
                "flow_m3_h": 1.892,
                "quantity": 20,
            },
        ],
        "connections": [
            {
                "from_id": "chiller-1",
                "to_id": "pump-chilled",
                "system": "chilled_water",
            },
            {
                "from_id": "pump-chilled",
                "to_id": "fcu-1",
                "system": "chilled_water",
            },
            {
                "from_id": "fcu-1",
                "to_id": "chiller-1",
                "system": "chilled_water",
            },
            {
                "from_id": "chiller-1",
                "to_id": "pump-cooling",
                "system": "cooling_water",
            },
            {
                "from_id": "pump-cooling",
                "to_id": "tower-1",
                "system": "cooling_water",
            },
            {
                "from_id": "tower-1",
                "to_id": "chiller-1",
                "system": "cooling_water",
            },
        ],
    }


def _save_project(client: TestClient, project: dict | None = None) -> str:
    response = client.post(
        "/api/v1/projects",
        json={"name": "teaching-test", "project": project or _teaching_project()},
    )
    assert response.status_code == 200
    return response.json()["project_id"]


def _compare(client: TestClient, project_id: str, scenario_id: str) -> dict:
    response = client.post(
        f"/api/v1/teaching/compare/{project_id}",
        json={"scenario_id": scenario_id},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_catalog_has_stable_order_effects_controls_and_copy_isolation(client):
    response = client.get("/api/v1/teaching/scenarios")

    assert response.status_code == 200
    catalog = response.json()
    assert [scenario["id"] for scenario in catalog] == SCENARIO_IDS
    assert all(scenario["name"] and scenario["description"] for scenario in catalog)
    assert all(scenario["controls"] for scenario in catalog)
    assert catalog[0]["effects"] == []
    assert all(scenario["effects"] for scenario in catalog[1:])

    first = list_teaching_scenarios()
    first[0].name = "changed by caller"
    assert list_teaching_scenarios()[0].name == "正常运行"


def test_normal_operation_with_nominal_stored_controls_has_no_fixed_expectations(
    client,
):
    project_id = _save_project(client)
    controls = client.get(f"/api/v1/realtime/controls/{project_id}").json()
    for device_id, device in controls["devices"].items():
        response = client.post(
            f"/api/v1/realtime/control/{project_id}",
            json={
                "device_instance_id": device_id,
                "values": {
                    name: control["current"]
                    for name, control in device["controls"].items()
                },
            },
        )
        assert response.status_code == 200

    body = _compare(client, project_id, "normal_operation")

    assert body["scenario"]["controls"]
    assert body["scenario"]["effects"] == []
    assert body["explanations"] == []
    assert all(
        metric["direction"] == "unchanged"
        for metric in body["metrics"].values()
        if metric["applicable"]
    )


def test_all_five_scenarios_run_and_return_complete_contract(client):
    project_id = _save_project(client)

    for scenario_id in SCENARIO_IDS:
        body = _compare(client, project_id, scenario_id)
        assert body["scenario"]["id"] == scenario_id
        assert list(body["metrics"]) == [
            "average_room_temp_c",
            "total_power_kw",
            "chiller_power_kw",
            "total_flow_m3_h",
            "delta_t_c",
            "condenser_water_temp_c",
            "system_cop",
        ]
        assert set(body["convergence"]) == {"baseline", "scenario"}
        assert all(
            {
                "code",
                "message",
                "related_devices",
                "related_metrics",
                "expected_direction",
                "observed_direction",
                "expectation_met",
            }
            <= set(explanation)
            for explanation in body["explanations"]
        )
    assert project_id not in realtime._sessions
    assert project_id not in realtime._layouts


def test_non_applicable_metrics_use_null_instead_of_zero(client):
    project_id = _save_project(
        client,
        {
            "rooms": [],
            "devices": [
                {
                    "id": "chiller-only",
                    "type": "冷源机组",
                    "name": "LG340MCF",
                }
            ],
            "connections": [],
        },
    )

    body = _compare(client, project_id, "chiller_capacity_low")

    room_temperature = body["metrics"]["average_room_temp_c"]
    assert room_temperature == {
        "before": None,
        "after": None,
        "change": None,
        "change_pct": None,
        "direction": None,
        "unit": "C",
        "applicable": False,
    }
    explanation = next(
        item
        for item in body["explanations"]
        if item["related_metrics"] == ["average_room_temp_c"]
    )
    assert explanation["observed_direction"] is None
    assert explanation["expectation_met"] is None


def test_scenario_physical_directions_and_capacity_alarm(client):
    project_id = _save_project(client)

    low_flow = _compare(client, project_id, "chilled_water_low_flow")
    assert low_flow["metrics"]["total_flow_m3_h"]["direction"] == "decrease"
    assert low_flow["metrics"]["delta_t_c"]["direction"] == "increase"

    low_cooling = _compare(client, project_id, "cooling_performance_low")
    assert (
        low_cooling["metrics"]["condenser_water_temp_c"]["direction"]
        == "increase"
    )
    assert low_cooling["metrics"]["system_cop"]["direction"] == "decrease"
    power = low_cooling["metrics"]["chiller_power_kw"]
    cop = low_cooling["metrics"]["system_cop"]
    if power["direction"] != "increase":
        # Lower condenser flow now also limits delivered cooling. The identity
        # P=Q/COP permits lower power when Q falls; that is not improved COP.
        assert power["after"] * cop["after"] < power["before"] * cop["before"]
        explanation = next(e for e in low_cooling["explanations"] if e["code"] == "COOLING_PERFORMANCE_LOW_CHILLER_POWER")
        assert explanation["expectation_met"] is None
    assert low_cooling["metrics"]["total_power_kw"]["direction"] in {
        "increase",
        "decrease",
        "unchanged",
    }

    low_capacity = _compare(client, project_id, "chiller_capacity_low")
    assert (
        "capacity_deficit" in low_capacity["scenario_result"]["alarms"]
        or low_capacity["metrics"]["average_room_temp_c"]["direction"]
        == "increase"
    )

    low_terminal = _compare(client, project_id, "terminal_capacity_low")
    assert (
        low_terminal["metrics"]["average_room_temp_c"]["direction"] == "increase"
    )


def test_non_applicable_and_unknown_resources_have_stable_errors(client):
    project_id = _save_project(
        client,
        {
            "rooms": [{"id": "room-only", "area_m2": 20}],
            "devices": [],
            "connections": [],
        },
    )

    unknown_project = client.post(
        "/api/v1/teaching/compare/not-found",
        json={"scenario_id": "normal_operation"},
    )
    assert unknown_project.status_code == 404
    assert unknown_project.json()["detail"] == "Project not found."

    unknown_scenario = client.post(
        f"/api/v1/teaching/compare/{project_id}",
        json={"scenario_id": "not-a-scenario"},
    )
    assert unknown_scenario.status_code == 404
    assert unknown_scenario.json()["detail"] == "Scenario not found."

    not_applicable = client.post(
        f"/api/v1/teaching/compare/{project_id}",
        json={"scenario_id": "cooling_performance_low"},
    )
    assert not_applicable.status_code == 422
    assert not_applicable.json()["detail"] == {
        "code": "SCENARIO_NOT_APPLICABLE",
        "scenario_id": "cooling_performance_low",
        "message": "Scenario is not applicable to this project.",
    }

    custom_conditions = client.post(
        f"/api/v1/teaching/compare/{project_id}",
        json={
            "scenario_id": "normal_operation",
            "before": {"pump-1": {"speed_pct": 100.0}},
            "after": {"pump-1": {"speed_pct": 45.0}},
        },
    )
    assert custom_conditions.status_code == 422
    assert {
        tuple(item["loc"])
        for item in custom_conditions.json()["detail"]
        if item["type"] == "extra_forbidden"
    } == {("body", "before"), ("body", "after")}


def test_comparison_is_deterministic_and_does_not_mutate_runtime(client):
    project_id = _save_project(client)
    config_response = client.post(
        f"/api/v1/realtime/simulate/config/{project_id}",
        json={
            "mode": "cooling",
            "interval_seconds": 2.0,
            "target_temp_c": 25.0,
            "outdoor_base_temp_c": 35.0,
            "fault_mode": "none",
        },
    )
    assert config_response.status_code == 200
    control_response = client.post(
        f"/api/v1/realtime/control/{project_id}",
        json={
            "device_instance_id": "pump-chilled",
            "values": {"speed_pct": 82.0},
        },
    )
    assert control_response.status_code == 200

    live_session = realtime._sessions[project_id]
    live_session.step()
    realtime._states[project_id] = {}
    realtime._summary[project_id] = {"sentinel": "state"}
    subscriber_sentinel = object()
    realtime._subscribers[project_id] = {subscriber_sentinel}
    history_path = realtime._history_path(project_id)
    history_path.parent.mkdir(parents=True, exist_ok=True)
    history_path.write_text('[{"sentinel": true}]', encoding="utf-8")
    project_path = project_store.PROJECT_DIR / f"{project_id}.json"

    tick_before = live_session.snapshot.tick
    snapshot_before = live_session.snapshot
    controls_before = {
        key: dict(values) for key, values in live_session.controls.items()
    }
    config_before = realtime._sim_configs[project_id]
    config_dump_before = config_before.model_dump()
    state_before = deepcopy(realtime._states[project_id])
    summary_before = deepcopy(realtime._summary[project_id])
    stored_controls_before = deepcopy(realtime._control_values[project_id])
    subscribers_before = realtime._subscribers[project_id]
    history_before = history_path.read_text(encoding="utf-8")
    project_before = project_path.read_bytes()
    layout_before = realtime._layouts[project_id]
    tasks_before = dict(realtime._sim_tasks)

    first = _compare(client, project_id, "chilled_water_low_flow")
    second = _compare(client, project_id, "chilled_water_low_flow")

    assert first == second
    assert first["metrics"]["total_flow_m3_h"]["before"] == pytest.approx(16.8182)
    assert realtime._sessions[project_id] is live_session
    assert realtime._layouts[project_id] is layout_before
    assert live_session.snapshot is snapshot_before
    assert live_session.snapshot.tick == tick_before
    assert {
        key: dict(values) for key, values in live_session.controls.items()
    } == controls_before
    assert realtime._sim_configs[project_id] is config_before
    assert config_before.model_dump() == config_dump_before
    assert realtime._states[project_id] == state_before
    assert realtime._summary[project_id] == summary_before
    assert realtime._control_values[project_id] == stored_controls_before
    assert realtime._subscribers[project_id] is subscribers_before
    assert history_path.read_text(encoding="utf-8") == history_before
    assert project_path.read_bytes() == project_before
    assert realtime._sim_tasks == tasks_before


def test_realtime_chiller_capacity_fault_uses_shared_validated_config(client):
    project_id = _save_project(client)
    start = client.post(
        f"/api/v1/realtime/simulate/start/{project_id}",
        json={
            "mode": "cooling",
            "interval_seconds": 10.0,
            "fault_mode": "chiller_capacity_low",
        },
    )
    try:
        assert start.status_code == 200
        session = realtime._sessions[project_id]
        assert (
            session.config.chiller_capacity_multiplier
            == CHILLER_CAPACITY_LOW_MULTIPLIER
        )
        state = client.get(f"/api/v1/realtime/state/{project_id}").json()
        assert "SIM_CAPACITY_DEFICIT" in {
            alarm["code"] for alarm in state["alarms"]
        }
    finally:
        client.post(f"/api/v1/realtime/simulate/stop/{project_id}")


@pytest.mark.parametrize(
    "multiplier", [0.0, -0.1, 1.01, float("nan"), float("inf"), float("-inf")]
)
def test_chiller_capacity_multiplier_validation(multiplier):
    with pytest.raises(ValueError, match="chiller capacity multiplier"):
        SimulationConfig(chiller_capacity_multiplier=multiplier)
