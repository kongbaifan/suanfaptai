"""Regression coverage for the Vue-first realtime teaching path.

These tests intentionally exercise the runtime adapter without invoking the
advanced static/PortGraph solver.  Static evaluation keeps its historical
strict validation contract; realtime startup must remain useful while a
student is still assembling an incomplete system.
"""

from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from app import main, realtime
from app.data import project_store
from app.main import app
from app.room_bindings import ProjectRoomBindingError
from app.samples import COMPLETE_SYSTEM_SAMPLE
from app.schemas import Issue, SystemEvaluateRequest
from app.simcore.project import build_project_layout, build_runtime_layout


def _create_project(client: TestClient, payload: dict) -> str:
    response = client.post(
        "/api/v1/projects", json={"name": "runtime-isolation", "project": payload}
    )
    assert response.status_code == 200, response.text
    return response.json()["project_id"]


def test_runtime_layout_preserves_binding_validation_and_nonfatal_diagnostics():
    payload = deepcopy(COMPLETE_SYSTEM_SAMPLE)
    payload["rooms"][0].pop("device_ids")
    project = SystemEvaluateRequest.model_validate(payload)

    strict = build_project_layout(project)
    resolved = build_runtime_layout(project)
    assert {issue.code for issue in resolved.issues} >= {"ROOM_BINDING_NO_DEVICE"}
    assert resolved.layout == strict.layout

    invalid = deepcopy(payload)
    invalid["rooms"][0]["device_ids"] = ["missing-terminal"]
    invalid_project = SystemEvaluateRequest.model_validate(invalid)
    with pytest.raises(ProjectRoomBindingError):
        build_runtime_layout(invalid_project)


def test_realtime_start_is_independent_of_advanced_solver(
    monkeypatch: pytest.MonkeyPatch, tmp_path
):
    monkeypatch.setattr(project_store, "PROJECT_DIR", tmp_path)
    monkeypatch.setattr(realtime, "HISTORY_DIR", tmp_path / "history")

    def fail_static_solver(*_args, **_kwargs):
        raise AssertionError("advanced static solver must not run for realtime")

    monkeypatch.setattr(main, "solve_steady_network", fail_static_solver)
    payload = deepcopy(COMPLETE_SYSTEM_SAMPLE)
    client = TestClient(app)
    project_id = _create_project(client, payload)
    try:
        started = client.post(f"/api/v1/realtime/simulate/start/{project_id}")
        assert started.status_code == 200, started.text
        state = client.get(f"/api/v1/realtime/state/{project_id}")
        assert state.status_code == 200
        assert state.json()["summary"]["simcore_tick"] == 1
    finally:
        client.post(f"/api/v1/realtime/simulate/stop/{project_id}")
        client.delete(f"/api/v1/projects/{project_id}")


def test_static_evaluation_degrades_when_advanced_solver_fails(
    monkeypatch: pytest.MonkeyPatch,
):
    def fail_static_solver(*_args, **_kwargs):
        raise RuntimeError("synthetic advanced solver failure")

    monkeypatch.setattr(main, "solve_steady_network", fail_static_solver)
    response = TestClient(app).post(
        "/api/v1/system/evaluate", json=deepcopy(COMPLETE_SYSTEM_SAMPLE)
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["load"] is not None
    assert body["steady_network"] is None
    assert any(
        issue["code"] == "STEADY_SOLVER_UNAVAILABLE"
        for issue in body["issues"]
    )


def test_static_evaluation_preserves_room_binding_422_from_solver(
    monkeypatch: pytest.MonkeyPatch,
):
    binding_issue = Issue(
        level="error",
        code="ROOM_BINDING_UNKNOWN",
        message="Room room_a references unknown device missing-terminal.",
        target="room_a",
    )

    def fail_with_binding_error(*_args, **_kwargs):
        raise ProjectRoomBindingError((binding_issue,))

    monkeypatch.setattr(main, "solve_steady_network", fail_with_binding_error)
    response = TestClient(app).post(
        "/api/v1/system/evaluate", json=deepcopy(COMPLETE_SYSTEM_SAMPLE)
    )

    assert response.status_code == 422, response.text
    assert response.json()["detail"] == [binding_issue.model_dump()]


def test_incomplete_water_topology_starts_with_bounded_zero_flow_and_issues(
    monkeypatch: pytest.MonkeyPatch, tmp_path
):
    monkeypatch.setattr(project_store, "PROJECT_DIR", tmp_path)
    monkeypatch.setattr(realtime, "HISTORY_DIR", tmp_path / "history")
    payload = deepcopy(COMPLETE_SYSTEM_SAMPLE)
    payload["connections"] = []
    client = TestClient(app)
    project_id = _create_project(client, payload)
    try:
        started = client.post(f"/api/v1/realtime/simulate/start/{project_id}")
        assert started.status_code == 200, started.text
        state = client.get(f"/api/v1/realtime/state/{project_id}").json()
        assert state["summary"]["simcore_tick"] == 1
        assert state["summary"]["chilled_water_flow_m3_h"] == 0.0
        assert state["summary"]["cooling_water_flow_m3_h"] == 0.0
        assert "SIM_NO_CHILLED_FLOW" in {
            issue["code"] for issue in state["alarms"]
        }
        assert "ISOLATED_PIPE" in {issue["code"] for issue in state["alarms"]}
    finally:
        client.post(f"/api/v1/realtime/simulate/stop/{project_id}")
        client.delete(f"/api/v1/projects/{project_id}")


def test_prestart_control_does_not_hide_incomplete_topology_diagnostics(
    monkeypatch: pytest.MonkeyPatch, tmp_path
):
    monkeypatch.setattr(project_store, "PROJECT_DIR", tmp_path)
    monkeypatch.setattr(realtime, "HISTORY_DIR", tmp_path / "history")
    payload = deepcopy(COMPLETE_SYSTEM_SAMPLE)
    payload["connections"] = []
    client = TestClient(app)
    project_id = _create_project(client, payload)
    try:
        controls = client.get(f"/api/v1/realtime/controls/{project_id}")
        assert controls.status_code == 200, controls.text
        started = client.post(f"/api/v1/realtime/simulate/start/{project_id}")
        assert started.status_code == 200, started.text
        state = client.get(f"/api/v1/realtime/state/{project_id}").json()
        assert "ISOLATED_PIPE" in {issue["code"] for issue in state["alarms"]}
    finally:
        client.post(f"/api/v1/realtime/simulate/stop/{project_id}")
        client.delete(f"/api/v1/projects/{project_id}")


def test_unknown_runtime_model_uses_explicit_degraded_parameters(
    monkeypatch: pytest.MonkeyPatch, tmp_path
):
    monkeypatch.setattr(project_store, "PROJECT_DIR", tmp_path)
    monkeypatch.setattr(realtime, "HISTORY_DIR", tmp_path / "history")
    payload = deepcopy(COMPLETE_SYSTEM_SAMPLE)
    payload["devices"][0]["name"] = "model-that-does-not-exist"
    client = TestClient(app)
    project_id = _create_project(client, payload)
    try:
        started = client.post(f"/api/v1/realtime/simulate/start/{project_id}")
        assert started.status_code == 200, started.text
        state = client.get(f"/api/v1/realtime/state/{project_id}").json()
        chiller = state["devices"]["chiller_1"]
        assert chiller["values"]["parameters_degraded"] is True
        assert "SIM_DEGRADED_PARAMETERS" in {
            issue["code"] for issue in state["alarms"]
        }
    finally:
        client.post(f"/api/v1/realtime/simulate/stop/{project_id}")
        client.delete(f"/api/v1/projects/{project_id}")


def test_zero_pump_flow_is_not_replaced_by_nominal_runtime_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path
):
    monkeypatch.setattr(project_store, "PROJECT_DIR", tmp_path)
    monkeypatch.setattr(realtime, "HISTORY_DIR", tmp_path / "history")
    client = TestClient(app)
    project_id = _create_project(client, deepcopy(COMPLETE_SYSTEM_SAMPLE))
    try:
        control = client.post(
            f"/api/v1/realtime/control/{project_id}",
            json={"device_instance_id": "pump_1", "values": {"speed_pct": 0}},
        )
        assert control.status_code == 200, control.text
        started = client.post(f"/api/v1/realtime/simulate/start/{project_id}")
        assert started.status_code == 200, started.text
        state = client.get(f"/api/v1/realtime/state/{project_id}").json()
        assert state["summary"]["chilled_water_flow_m3_h"] == 0.0
        assert state["summary"]["total_flow_m3_h"] == 0.0
        assert state["devices"]["pump_1"]["values"]["flow_m3_h"] == 0.0
    finally:
        client.post(f"/api/v1/realtime/simulate/stop/{project_id}")
        client.delete(f"/api/v1/projects/{project_id}")


def test_incomplete_runtime_snapshot_is_identical_over_websocket_and_rest(
    monkeypatch: pytest.MonkeyPatch, tmp_path
):
    monkeypatch.setattr(project_store, "PROJECT_DIR", tmp_path)
    monkeypatch.setattr(realtime, "HISTORY_DIR", tmp_path / "history")
    payload = deepcopy(COMPLETE_SYSTEM_SAMPLE)
    payload["connections"] = []
    client = TestClient(app)
    project_id = _create_project(client, payload)
    try:
        with client.websocket_connect(f"/ws/realtime/{project_id}") as websocket:
            assert websocket.receive_json()["type"] == "connected"
            started = client.post(f"/api/v1/realtime/simulate/start/{project_id}")
            assert started.status_code == 200
            message = websocket.receive_json()
            while message["type"] != "system_tick":
                message = websocket.receive_json()
            rest = client.get(f"/api/v1/realtime/state/{project_id}")
            assert rest.status_code == 200
            assert message["data"] == rest.json()
    finally:
        client.post(f"/api/v1/realtime/simulate/stop/{project_id}")
        client.delete(f"/api/v1/projects/{project_id}")
