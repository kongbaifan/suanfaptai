"""Public control acknowledgements and one immutable REST/WS calculation."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Event

import pytest
from fastapi.testclient import TestClient

from app import realtime
from app.main import app
from app.samples import SAMPLES
from app.schemas import SystemEvaluateRequest
from app.simcore.project import build_runtime_layout
from app.simcore.session import SimulationSession
from app.simcore.synchronization import project_runtime_lock


@pytest.fixture
def running_network():
    payload = deepcopy(SAMPLES["four_floor_fcu_fresh_air"])
    payload["devices"].append({"id": "valve", "type": "球阀", "name": "Q11F-16T DN25"})
    edge = next(
        e
        for e in payload["connections"]
        if e["from_id"] == "chw_branch_supply_f1" and e["to_id"] == "fcu_f1"
    )
    edge["to_id"] = "valve"
    payload["connections"].append(
        {"from_id": "valve", "to_id": "fcu_f1", "system": "chilled_water"}
    )
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/projects", json={"name": "network-contract", "project": payload}
        )
        assert response.status_code == 200, response.text
        project_id = response.json()["project_id"]
        try:
            started = client.post(
                f"/api/v1/realtime/simulate/start/{project_id}",
                json={"interval_seconds": 3600},
            )
            assert started.status_code == 200, started.text
            yield client, project_id
        finally:
            client.post(f"/api/v1/realtime/simulate/stop/{project_id}")
            client.delete(f"/api/v1/projects/{project_id}")


def _step(project_id):
    with project_runtime_lock(project_id):
        project = realtime.get_project(project_id).project
        state = realtime._simulate_system_tick(
            project_id,
            project,
            realtime._sessions[project_id],
            realtime.get_simulation_config(project_id),
        )
        realtime._store_system_state(project_id, state)


def test_accepted_control_is_applied_on_the_next_tick(running_network):
    client, project_id = running_network
    before = client.get(f"/api/v1/realtime/state/{project_id}").json()
    accepted = client.post(
        f"/api/v1/realtime/control/{project_id}",
        json={"device_instance_id": "valve", "values": {"opening_pct": 0}},
    ).json()
    assert accepted["control_revision"] > before["applied_control_revision"]
    assert accepted["session_id"] == before["session_id"]
    assert accepted["devices"]["valve"]["controls"]["opening_pct"]["current"] == 0
    assert accepted["devices"]["valve"]["controls"]["opening_pct"]["effective"] == 100
    pending = client.get(f"/api/v1/realtime/state/{project_id}").json()
    assert pending == before
    _step(project_id)
    after = client.get(f"/api/v1/realtime/state/{project_id}").json()
    assert after["applied_control_revision"] == accepted["control_revision"]
    assert after["simcore_tick"] == before["simcore_tick"] + 1
    assert after["devices"]["fcu_f1"]["values"]["water_flow_m3_h"] == 0
    assert after["devices"]["fcu_f2"]["values"]["water_flow_m3_h"] > 0


def test_same_snapshot_is_used_by_rest_views_and_websocket(running_network):
    client, project_id = running_network
    state = client.get(f"/api/v1/realtime/state/{project_id}").json()
    for endpoint in ("devices", "pipes", "rooms"):
        body = client.get(f"/api/v1/realtime/{endpoint}/{project_id}").json()
        assert (
            body["session_id"],
            body["simcore_tick"],
            body["applied_control_revision"],
        ) == (
            state["session_id"],
            state["simcore_tick"],
            state["applied_control_revision"],
        )
    pipes = client.get(f"/api/v1/realtime/pipes/{project_id}").json()["pipes"]
    solved = {pipe["node_id"]: pipe for pipe in state["network"]["pipes"]}
    for pipe in pipes:
        assert pipe["flow_m3_h"] == pytest.approx(
            solved[pipe["pipe_id"]]["flow_m3_h"], abs=0.0005
        )
    with client.websocket_connect(f"/ws/realtime/{project_id}") as ws:
        message = ws.receive_json()
        assert message["type"] == "connected"
        assert message["data"] == state


def test_external_updates_are_rejected_without_changing_snapshot(running_network):
    client, project_id = running_network
    before = client.get(f"/api/v1/realtime/state/{project_id}").json()
    response = client.post(
        "/api/v1/realtime/update",
        json={
            "project_id": project_id,
            "states": [
                {
                    "project_id": project_id,
                    "device_id": "fcu_f1",
                    "device_type": "风机盘管",
                    "values": {"water_flow_m3_h": 999},
                }
            ],
        },
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "RUNTIME_STATE_REQUIRES_STOP"
    assert client.get(f"/api/v1/realtime/state/{project_id}").json() == before


def test_invalid_control_is_atomic_and_does_not_increment_revision(running_network):
    client, project_id = running_network
    before = client.get(f"/api/v1/realtime/controls/{project_id}").json()
    response = client.post(
        f"/api/v1/realtime/control/{project_id}",
        json={
            "device_instance_id": "valve",
            "values": {"opening_pct": 0, "target_temp_c": 24},
        },
    )
    assert response.status_code == 422
    assert client.get(f"/api/v1/realtime/controls/{project_id}").json() == before


def test_rebuilding_session_does_not_reuse_old_tick_identity(running_network):
    client, project_id = running_network
    previous = client.get(f"/api/v1/realtime/state/{project_id}").json()
    client.post(f"/api/v1/realtime/simulate/stop/{project_id}")
    response = client.post(
        f"/api/v1/realtime/simulate/config/{project_id}", json={"mode": "heating"}
    )
    assert response.status_code == 200
    idle = client.get(f"/api/v1/realtime/rooms/{project_id}").json()
    assert idle["session_id"] is None and idle["simcore_tick"] == 0
    assert (
        client.get(f"/api/v1/realtime/state/{project_id}").json()["session_id"] is None
    )
    client.post(
        f"/api/v1/realtime/simulate/start/{project_id}",
        json={"mode": "heating", "interval_seconds": 3600},
    )
    current = client.get(f"/api/v1/realtime/state/{project_id}").json()
    assert current["session_id"] != previous["session_id"]
    assert current["simcore_tick"] == 1


def test_control_cannot_mutate_a_calculation_in_progress(monkeypatch):
    session = SimulationSession(
        build_runtime_layout(
            SystemEvaluateRequest.model_validate(SAMPLES["complete"])
        ).layout
    )
    device_id = session.layout.devices[0].instance_id
    entered, release, command_started = Event(), Event(), Event()
    original = session._solve

    def paused_solve(ambient):
        entered.set()
        assert release.wait(3)
        return original(ambient)

    def command():
        command_started.set()
        return session.set_controls(device_id, {"enabled": 0})

    monkeypatch.setattr(session, "_solve", paused_solve)
    with ThreadPoolExecutor(max_workers=2) as pool:
        stepping = pool.submit(session.step)
        assert entered.wait(3)
        writing = pool.submit(command)
        assert command_started.wait(3)
        assert not writing.done()
        release.set()
        snapshot = stepping.result(timeout=3)
        writing.result(timeout=3)
    assert snapshot.applied_control_revision == 0
    assert session.control_revision == 1
    assert snapshot.control_sources[device_id] == "auto"
