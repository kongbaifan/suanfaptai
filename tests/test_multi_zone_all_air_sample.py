import math
from copy import deepcopy
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import realtime
from app.data import project_store
from app.main import app
from app.room_bindings import resolve_room_bindings
from app.samples import MULTI_ZONE_ALL_AIR_SAMPLE
from app.schemas import SystemEvaluateRequest
from app.simcore.project import build_project_layout
from app.simcore.session import SimulationConfig, SimulationMode, SimulationSession
from app.simcore.steady import solve_steady

client = TestClient(app)
SAMPLE_PATH = "/api/v1/project/sample/multi_zone_all_air"
ROOM_IDS = ("zone_office", "zone_meeting", "zone_training")


def _sample() -> dict:
    return deepcopy(MULTI_ZONE_ALL_AIR_SAMPLE)


def _binding_payload(
    device_ids: list[str] | None,
    *,
    device_type: str = "组合式空调机组",
    rooms: list[dict] | None = None,
) -> dict:
    """房间侧持有设备清单，device_ids 只挂在第一个房间上。"""
    room_list = deepcopy(
        rooms
        or [
            {"id": "room_a", "floor_id": "F1", "area_m2": 30},
            {"id": "room_b", "floor_id": "F1", "area_m2": 40},
        ]
    )
    if device_ids is not None:
        room_list[0]["device_ids"] = device_ids
    return {
        "rooms": room_list,
        "devices": [{"id": "terminal_1", "type": device_type}],
    }


@pytest.mark.parametrize(
    ("payload", "expected_code", "expected_target"),
    [
        (
            _binding_payload(["terminal_1", "terminal_1"]),
            "ROOM_BINDING_DUPLICATE",
            "room_a",
        ),
        (_binding_payload(["missing"]), "ROOM_BINDING_UNKNOWN", "room_a"),
        (
            _binding_payload(["terminal_1"], device_type="冷源机组"),
            "ROOM_BINDING_UNSUPPORTED_DEVICE",
            "room_a",
        ),
    ],
)
def test_binding_validation_reports_stable_error_codes(
    payload: dict, expected_code: str, expected_target: str
):
    response = client.post("/api/v1/project/validate", json=payload)

    assert response.status_code == 200
    assert response.json()["valid"] is False
    issues = response.json()["issues"]
    assert expected_code in {issue["code"] for issue in issues}
    assert any(
        issue["code"] == expected_code and issue["target"] == expected_target
        for issue in issues
    )


def test_unbound_room_and_unclaimed_terminal_are_warnings_not_errors():
    """房间无末端、末端无房间都只警告——旧实现按数组位置兜底把这两种情况静默吞掉了。"""
    payload = _binding_payload(None)
    response = client.post("/api/v1/project/validate", json=payload)

    assert response.status_code == 200
    body = response.json()
    codes = {issue["code"]: issue["level"] for issue in body["issues"]}
    assert codes.get("ROOM_BINDING_NO_DEVICE") == "warning"
    assert codes.get("ROOM_BINDING_DEVICE_UNCLAIMED") == "warning"
    # 该 payload 另有 MISSING_COLD_SOURCE 等错误，只断言绑定本身不再产生 error。
    assert not [
        issue
        for issue in body["issues"]
        if issue["level"] == "error" and issue["code"].startswith("ROOM_BINDING_")
    ]

    resolution = resolve_room_bindings(SystemEvaluateRequest(**payload))
    assert resolution.room_ids_by_device["terminal_1"] == ()
    assert resolution.errors == ()


def test_one_terminal_serves_every_room_that_lists_it():
    payload = _binding_payload(["terminal_1"])
    payload["rooms"][1]["device_ids"] = ["terminal_1"]

    resolution = resolve_room_bindings(SystemEvaluateRequest(**payload))

    assert resolution.room_ids_by_device["terminal_1"] == ("room_a", "room_b")
    assert resolution.issues == ()


def test_sample_validates_evaluates_and_builds_one_branch_per_water_loop():
    response = client.get(SAMPLE_PATH)
    assert response.status_code == 200
    payload = response.json()
    assert [room["device_ids"] for room in payload["rooms"]] == [["ahu_f1"]] * 3

    validation = client.post("/api/v1/project/validate", json=payload)
    assert validation.status_code == 200
    assert validation.json()["valid"] is True
    assert validation.json()["issues"] == []

    evaluation = client.post("/api/v1/system/evaluate", json=payload)
    assert evaluation.status_code == 200
    body = evaluation.json()
    assert not [issue for issue in body["issues"] if issue["level"] == "error"]
    assert body["load"]["adjusted_cooling_kw"] > 0.0
    assert body["load"]["adjusted_heating_kw"] > 0.0
    assert body["air_network"]["total_airflow_m3_h"] > 0.0
    assert body["air_network"]["paths"]
    assert body["air_network"]["most_unfavorable_path_id"]
    assert body["air_network"]["required_fan_pressure_pa"] > 0.0

    layout = build_project_layout(SystemEvaluateRequest(**payload)).layout
    ahu = next(node for node in layout.devices if node.instance_id == "ahu_f1")
    assert ahu.effective_room_ids == ROOM_IDS
    assert {
        (branch.loop, branch.terminal_instance_id)
        for branch in layout.hydronic_branches
    } == {("chilled", "ahu_f1"), ("hot", "ahu_f1")}


def test_save_round_trip_preserves_binding_order_and_rejects_invalid_write(
    monkeypatch: pytest.MonkeyPatch, tmp_path
):
    monkeypatch.setattr(project_store, "PROJECT_DIR", tmp_path)
    payload = _sample()
    response = client.post(
        "/api/v1/projects", json={"name": "multi-zone", "project": payload}
    )
    assert response.status_code == 200
    project_id = response.json()["project_id"]
    loaded = client.get(f"/api/v1/projects/{project_id}")
    assert [
        room["device_ids"] for room in loaded.json()["project"]["rooms"]
    ] == [["ahu_f1"]] * 3

    before = set(tmp_path.glob("*.json"))
    invalid = _sample()
    invalid["rooms"][0]["device_ids"] = ["ahu_missing"]
    rejected = client.post(
        "/api/v1/projects", json={"name": "invalid", "project": invalid}
    )
    assert rejected.status_code == 422
    assert rejected.json()["detail"][0]["code"] == "ROOM_BINDING_UNKNOWN"
    assert set(tmp_path.glob("*.json")) == before


@pytest.mark.parametrize(
    "path",
    ["/api/v1/system/evaluate", "/api/v1/simulation/steady-state"],
)
def test_session_backed_http_endpoints_project_binding_errors_as_422(path: str):
    response = client.post(path, json=_binding_payload(["missing"]))

    assert response.status_code == 422
    assert response.json()["detail"][0]["code"] == "ROOM_BINDING_UNKNOWN"


def test_invalid_binding_creates_no_realtime_session_or_control_state(
    monkeypatch: pytest.MonkeyPatch,
):
    project_id = "invalid-binding-project"
    project = SystemEvaluateRequest(**_binding_payload(["missing"]))
    monkeypatch.setattr(
        realtime,
        "get_project",
        lambda _project_id: SimpleNamespace(project=project),
    )

    response = client.post(
        f"/api/v1/realtime/control/{project_id}",
        json={"device_instance_id": "terminal_1", "values": {}},
    )

    assert response.status_code == 422
    assert response.json()["detail"][0]["code"] == "ROOM_BINDING_UNKNOWN"
    assert project_id not in realtime._sessions
    assert project_id not in realtime._layouts
    assert project_id not in realtime._control_values


@pytest.mark.parametrize(
    ("mode", "initial_temp", "target_temp", "expected_load"),
    [
        (SimulationMode.COOLING, 30.0, 26.0, "cooling_kw"),
        (SimulationMode.HEATING, 16.0, 22.0, "heating_kw"),
        (SimulationMode.VENTILATION, 26.0, 26.0, None),
    ],
)
def test_multi_room_allocation_is_mode_weighted_and_conserved(
    mode: SimulationMode,
    initial_temp: float,
    target_temp: float,
    expected_load: str | None,
):
    layout = build_project_layout(SystemEvaluateRequest(**_sample())).layout
    session = SimulationSession(
        layout,
        SimulationConfig(
            mode=mode,
            initial_room_temp_c=initial_temp,
            target_temp_c=target_temp,
            outdoor_base_temp_c=35.0 if mode is SimulationMode.COOLING else 5.0,
            automatic_control=False,
        ),
    )
    snapshot = session.step()
    ahu = snapshot.devices["ahu_f1"].values
    room_readings = [snapshot.rooms[room_id] for room_id in ROOM_IDS]

    assert math.fsum(room.airflow_m3_h for room in room_readings) == ahu[
        "airflow_m3_h"
    ]
    # 房间 cooling_kw 是**显热**：制冷时盘管按"混合风过盘管"算出送风状态，
    # 显热 = 风量·c_p·(室温 − 送风温)，潜热走湿度平衡。显热不再是全热乘固定
    # 显热比，故只能与全热比较量级。制热与通风不拆分，原样传递。
    room_sensible_kw = math.fsum(room.cooling_kw for room in room_readings)
    if mode is SimulationMode.COOLING:
        # 显热不超过盘管全热（潜热非负），且应在合理量级内
        assert 0.0 <= room_sensible_kw <= ahu["cooling_kw"] + 0.01
        assert room_sensible_kw >= ahu["cooling_kw"] * 0.5
    else:
        assert room_sensible_kw == pytest.approx(ahu["cooling_kw"])
    assert math.fsum(room.heating_kw for room in room_readings) == ahu[
        "heating_kw"
    ]
    assert list(snapshot.devices).count("ahu_f1") == 1

    room_nodes = {room.room_id: room for room in layout.rooms}
    if mode is SimulationMode.HEATING:
        raw_weights = [room_nodes[room_id].design_heating_kw for room_id in ROOM_IDS]
    elif mode is SimulationMode.VENTILATION:
        raw_weights = [room_nodes[room_id].fresh_air_m3_h for room_id in ROOM_IDS]
    else:
        raw_weights = [room_nodes[room_id].design_cooling_kw for room_id in ROOM_IDS]
    total_weight = math.fsum(raw_weights)
    for room, weight in zip(room_readings[:-1], raw_weights[:-1], strict=True):
        assert room.airflow_m3_h == pytest.approx(
            ahu["airflow_m3_h"] * weight / total_weight
        )
        # 风量与制热按权重精确分摊；制冷时房间读数是显热，由盘管出风状态
        # 算出，不按固定显热比分摊，因此只校验风量这一条守恒关系。
        if expected_load is not None and expected_load != "cooling_kw":
            assert getattr(room, expected_load) == pytest.approx(
                ahu[expected_load] * weight / total_weight
            )
    if expected_load is None:
        # 通风既不制冷也不制热，只送风。
        assert ahu["cooling_kw"] == 0.0
        assert ahu["heating_kw"] == 0.0
    else:
        # 目录缺额定风量/水流量的条目现在由 rated.py 兜底回填，机组因此出力，
        # 但补齐值仍标记为降级，告警必须保留——出力有了不等于参数可信。
        assert ahu[expected_load] > 0.0
        assert "degraded_parameters" in snapshot.alarms


@pytest.mark.parametrize(
    ("mode", "initial_temp", "target_temp"),
    [
        (SimulationMode.COOLING, 30.0, 26.0),
        (SimulationMode.HEATING, 16.0, 22.0),
    ],
)
def test_multi_zone_sample_completes_shared_steady_solve(
    mode: SimulationMode, initial_temp: float, target_temp: float
):
    layout = build_project_layout(SystemEvaluateRequest(**_sample())).layout
    result = solve_steady(
        layout,
        SimulationConfig(
            mode=mode,
            initial_room_temp_c=initial_temp,
            target_temp_c=target_temp,
            outdoor_base_temp_c=35.0 if mode is SimulationMode.COOLING else 5.0,
            automatic_control=False,
        ),
    )

    assert result.ticks <= 500
    assert result.converged
    # 兜底回填前这些量恒为 0（目录条目缺额定风量/水流量），全空气样例因此
    # 整条水路都不动。现在机组有额定值，稳态必须解出正的流量与负荷。
    if mode is SimulationMode.COOLING:
        assert result.snapshot.chilled_flow_m3_h > 0.0
        assert result.snapshot.cooling_load_kw > 0.0
        assert result.snapshot.hot_water_flow_m3_h == 0.0
    else:
        assert result.snapshot.hot_water_flow_m3_h > 0.0
        assert result.snapshot.heating_load_kw > 0.0
        assert result.snapshot.chilled_flow_m3_h == 0.0
    assert "degraded_parameters" in result.snapshot.alarms


def test_realtime_room_projection_uses_authoritative_multi_room_snapshot(
    monkeypatch: pytest.MonkeyPatch, tmp_path
):
    monkeypatch.setattr(project_store, "PROJECT_DIR", tmp_path)
    monkeypatch.setattr(realtime, "HISTORY_DIR", tmp_path / "realtime_history")
    created = client.post(
        "/api/v1/projects",
        json={"name": "multi-zone-realtime", "project": _sample()},
    )
    assert created.status_code == 200
    project_id = created.json()["project_id"]
    try:
        started = client.post(
            f"/api/v1/realtime/simulate/start/{project_id}",
            json={
                "mode": "cooling",
                "initial_room_temp_c": 30.0,
                "target_temp_c": 26.0,
            },
        )
        assert started.status_code == 200
        room_response = client.get(f"/api/v1/realtime/rooms/{project_id}")
        state_response = client.get(f"/api/v1/realtime/state/{project_id}")
        assert room_response.status_code == 200
        assert state_response.status_code == 200
        rooms = room_response.json()["rooms"]
        state = state_response.json()
        assert room_response.json()["simcore_tick"] == state["summary"]["simcore_tick"]
        assert [room["room_id"] for room in rooms] == list(ROOM_IDS)
        assert all(
            {"airflow_m3_h", "cooling_kw", "heating_kw"} <= set(room)
            for room in rooms
        )
        ahu = state["devices"]["ahu_f1"]["values"]
        assert sum(room["airflow_m3_h"] for room in rooms) == pytest.approx(
            ahu["airflow_m3_h"], abs=0.003
        )
        # 房间 cooling_kw 是显热、机组 cooling_kw 是全热，见上面同名说明。
        # 显热由盘管出风状态算出，不再等于全热的固定份额，只校验不超全热。
        room_sensible_kw = sum(room["cooling_kw"] for room in rooms)
        assert 0.0 <= room_sensible_kw <= ahu["cooling_kw"] + 0.005
    finally:
        client.post(f"/api/v1/realtime/simulate/stop/{project_id}")
        realtime._sessions.pop(project_id, None)
