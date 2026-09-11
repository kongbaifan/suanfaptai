from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import realtime
from app.data import project_store
from app.main import app
from app.schemas import RealtimeRoomStatesResult, RealtimeSimulationConfig


client = TestClient(app)


def _create_project():
    sample = client.get("/api/v1/project/sample/complete").json()
    response = client.post(
        "/api/v1/projects", json={"name": "pytest_realtime", "project": sample}
    )
    assert response.status_code == 200
    return response.json()["project_id"], sample


def _create_four_floor_project():
    sample = client.get(
        "/api/v1/project/sample/four_floor_fcu_fresh_air"
    ).json()
    response = client.post(
        "/api/v1/projects",
        json={"name": "pytest_realtime_heating", "project": sample},
    )
    assert response.status_code == 200
    return response.json()["project_id"], sample


def test_realtime_update_state_and_history():
    project_id, sample = _create_project()
    try:
        device_id = sample["devices"][1]["id"]
        response = client.post(
            "/api/v1/realtime/update",
            json={
                "project_id": project_id,
                "states": [
                    {
                        "project_id": project_id,
                        "device_id": device_id,
                        "status": "running",
                        "values": {"flow_m3_h": 40, "head_m": 28, "power_kw": 6.5},
                    }
                ],
            },
        )

        assert response.status_code == 200
        body = response.json()
        assert body["devices"][device_id]["values"]["flow_m3_h"] == 40
        assert body["history_count"] >= 1

        state_response = client.get(f"/api/v1/realtime/state/{project_id}")
        assert state_response.status_code == 200
        assert device_id in state_response.json()["devices"]

        history_response = client.get(f"/api/v1/realtime/history/{project_id}")
        assert history_response.status_code == 200
        assert history_response.json()["points"]
    finally:
        client.delete(f"/api/v1/projects/{project_id}")
    assert not (realtime.HISTORY_DIR / f"{project_id}.json").exists()


def test_realtime_detects_stale_and_low_flow_alarms():
    project_id, sample = _create_project()
    try:
        device_id = sample["devices"][1]["id"]
        stale_time = (
            (datetime.now(UTC) - timedelta(seconds=30))
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )
        response = client.post(
            "/api/v1/realtime/update",
            json={
                "project_id": project_id,
                "states": [
                    {
                        "project_id": project_id,
                        "device_id": device_id,
                        "timestamp": stale_time,
                        "status": "running",
                        "values": {"flow_m3_h": 1, "head_m": 10, "power_kw": 0.5},
                    }
                ],
            },
        )

        codes = {issue["code"] for issue in response.json()["alarms"]}
        assert "DEVICE_OFFLINE" in codes
        assert "FLOW_LOW" in codes
    finally:
        client.delete(f"/api/v1/projects/{project_id}")


def test_realtime_simulation_start_stop_and_websocket():
    project_id, _ = _create_project()
    try:
        with client.websocket_connect(f"/ws/realtime/{project_id}") as websocket:
            first = websocket.receive_json()
            assert first["type"] == "connected"

            start = client.post(f"/api/v1/realtime/simulate/start/{project_id}")
            assert start.status_code == 200
            assert start.json()["running"] is True

            message = websocket.receive_json()
            assert message["type"] in {
                "simulation_status",
                "device_state_updated",
                "system_recalculated",
                "system_tick",
            }

            stop = client.post(f"/api/v1/realtime/simulate/stop/{project_id}")
            assert stop.status_code == 200
            assert stop.json()["running"] is False
    finally:
        client.delete(f"/api/v1/projects/{project_id}")


def test_realtime_simulation_config_endpoint_and_system_tick_shape():
    project_id, sample = _create_project()
    try:
        config = {
            "mode": "cooling",
            "interval_seconds": 0.2,
            "target_temp_c": 25.5,
            "outdoor_base_temp_c": 36,
            "fault_mode": "terminal_capacity_low",
        }
        set_response = client.post(
            f"/api/v1/realtime/simulate/config/{project_id}", json=config
        )
        assert set_response.status_code == 200
        assert set_response.json()["target_temp_c"] == 25.5

        get_response = client.get(f"/api/v1/realtime/simulate/config/{project_id}")
        assert get_response.status_code == 200
        assert get_response.json()["fault_mode"] == "terminal_capacity_low"

        start = client.post(
            f"/api/v1/realtime/simulate/start/{project_id}", json=config
        )
        assert start.status_code == 200
        assert start.json()["interval_seconds"] == 0.2

        state = client.get(f"/api/v1/realtime/state/{project_id}").json()
        assert set(state["devices"]) == {device["id"] for device in sample["devices"]}
        assert state["summary"]["mode"] == "cooling"
        assert state["summary"]["target_room_temp_c"] == 25.5
        assert "floor_states" in state["summary"]
        assert "control_state" in state["summary"]
        assert "chiller_load_ratio" in state["summary"]

        history = client.get(f"/api/v1/realtime/history/{project_id}").json()
        assert history["points"][-1]["floor_states"]
        assert (
            history["points"][-1]["control_state"]["fault_mode"]
            == "terminal_capacity_low"
        )

        stop = client.post(f"/api/v1/realtime/simulate/stop/{project_id}")
        assert stop.status_code == 200
    finally:
        client.post(f"/api/v1/realtime/simulate/stop/{project_id}")
        client.delete(f"/api/v1/projects/{project_id}")


def test_realtime_simulation_config_validates_temperature_contract():
    project_id, _ = _create_project()
    try:
        default_config = client.get(
            f"/api/v1/realtime/simulate/config/{project_id}"
        )
        assert default_config.status_code == 200
        assert default_config.json()["initial_room_temp_c"] is None
        default_rooms = client.get(f"/api/v1/realtime/rooms/{project_id}")
        assert default_rooms.status_code == 200
        assert {
            room["temperature_c"] for room in default_rooms.json()["rooms"]
        } == {30.0}
        assert {
            room["target_temp_c"] for room in default_rooms.json()["rooms"]
        } == {26.0}

        for initial_temp_c, target_temp_c in ((10.0, 40.0), (40.0, 10.0)):
            response = client.post(
                f"/api/v1/realtime/simulate/config/{project_id}",
                json={
                    "initial_room_temp_c": initial_temp_c,
                    "target_temp_c": target_temp_c,
                },
            )
            assert response.status_code == 200
            assert response.json()["initial_room_temp_c"] == initial_temp_c
            assert response.json()["target_temp_c"] == target_temp_c

        invalid_payloads = (
            {"initial_room_temp_c": 9.99},
            {"initial_room_temp_c": 40.01},
            {"target_temp_c": 9.99},
            {"target_temp_c": 40.01},
            {"unexpected": True},
        )
        for payload in invalid_payloads:
            response = client.post(
                f"/api/v1/realtime/simulate/config/{project_id}", json=payload
            )
            assert response.status_code == 422

        numeric_fields = (
            "initial_room_temp_c",
            "target_temp_c",
            "outdoor_base_temp_c",
            "outdoor_relative_humidity_pct",
            "interval_seconds",
        )
        for value in (float("nan"), float("inf"), float("-inf")):
            for field in numeric_fields:
                with pytest.raises(ValueError):
                    RealtimeSimulationConfig.model_validate({field: value})
        for encoded_value in ("NaN", "Infinity", "-Infinity"):
            for field in numeric_fields:
                response = client.post(
                    f"/api/v1/realtime/simulate/config/{project_id}",
                    json={field: encoded_value},
                )
                assert response.status_code == 422
    finally:
        client.delete(f"/api/v1/projects/{project_id}")


def test_room_initial_conditions_are_normalized_and_validation_is_atomic():
    project_id, _ = _create_project()
    initial = {
        "initial_room_temp_c": 29.0,
        "room_initial_conditions": [
            {
                "room_id": "meeting_1",
                "temperature_c": 27.0,
                "relative_humidity_pct": 62.0,
            },
            {
                "room_id": "office_1",
                "temperature_c": 31.0,
                "relative_humidity_pct": 48.0,
            },
        ],
    }
    try:
        accepted = client.post(
            f"/api/v1/realtime/simulate/config/{project_id}", json=initial
        )
        assert accepted.status_code == 200
        assert [
            item["room_id"] for item in accepted.json()["room_initial_conditions"]
        ] == ["office_1", "meeting_1"]
        rooms = client.get(f"/api/v1/realtime/rooms/{project_id}").json()["rooms"]
        assert {
            room["room_id"]: (
                room["temperature_c"], room["relative_humidity_pct"]
            )
            for room in rooms
        } == {"office_1": (31.0, 48.0), "meeting_1": (27.0, 62.0)}

        before_config = client.get(
            f"/api/v1/realtime/simulate/config/{project_id}"
        ).json()
        session_before = realtime._sessions.get(project_id)
        for payload, code in (
            (
                {
                    **initial,
                    "room_initial_conditions": [
                        initial["room_initial_conditions"][0],
                        initial["room_initial_conditions"][0],
                    ],
                },
                "RUNTIME_INITIAL_ROOM_DUPLICATE",
            ),
            (
                {
                    **initial,
                    "room_initial_conditions": [
                        {
                            "room_id": "missing",
                            "temperature_c": 25.0,
                            "relative_humidity_pct": 50.0,
                        }
                    ],
                },
                "RUNTIME_INITIAL_ROOM_UNKNOWN",
            ),
        ):
            rejected = client.post(
                f"/api/v1/realtime/simulate/config/{project_id}", json=payload
            )
            assert rejected.status_code == 422
            assert rejected.json()["detail"]["code"] == code
            assert client.get(
                f"/api/v1/realtime/simulate/config/{project_id}"
            ).json() == before_config
            assert realtime._sessions.get(project_id) is session_before
    finally:
        client.delete(f"/api/v1/projects/{project_id}")


def test_project_indoor_humidity_accepts_only_teaching_range():
    sample = client.get("/api/v1/project/sample/complete").json()
    created_project_ids = []
    try:
        for relative_humidity in (0.1, 0.9):
            for room in sample["rooms"]:
                room["indoor_relative_humidity"] = relative_humidity
            response = client.post(
                "/api/v1/projects",
                json={"name": "humidity_boundary", "project": sample},
            )
            assert response.status_code == 200
            created_project_ids.append(response.json()["project_id"])

        for relative_humidity in (0.099, 0.901):
            for room in sample["rooms"]:
                room["indoor_relative_humidity"] = relative_humidity
            response = client.post(
                "/api/v1/projects",
                json={"name": "humidity_invalid", "project": sample},
            )
            if response.status_code == 200:
                created_project_ids.append(response.json()["project_id"])
            assert response.status_code == 422
    finally:
        for project_id in created_project_ids:
            client.delete(f"/api/v1/projects/{project_id}")


def test_room_states_return_typed_initial_snapshot_without_runtime_side_effects():
    sample = client.get("/api/v1/project/sample/complete").json()
    sample["floors"] = [
        {"id": "F2", "name": "Second floor", "level": 2},
        {"id": "F1", "name": "First floor", "level": 1},
    ]
    sample["rooms"][0].update(
        {"id": "room_b", "floor_id": "F2", "indoor_relative_humidity": 0.556}
    )
    sample["rooms"][1].update(
        {"id": None, "floor_id": None, "indoor_relative_humidity": 0.644}
    )
    response = client.post(
        "/api/v1/projects",
        json={"name": "room_state_order", "project": sample},
    )
    assert response.status_code == 200
    project_id = response.json()["project_id"]
    try:
        config_response = client.post(
            f"/api/v1/realtime/simulate/config/{project_id}",
            json={"initial_room_temp_c": 31.234, "target_temp_c": 24.678},
        )
        assert config_response.status_code == 200
        project_before = client.get(f"/api/v1/projects/{project_id}").json()
        project_path = project_store.PROJECT_DIR / f"{project_id}.json"
        project_bytes_before = project_path.read_bytes()
        project_mtime_before = project_path.stat().st_mtime_ns
        config_before = realtime._sim_configs[project_id].model_copy(deep=True)

        room_response = client.get(f"/api/v1/realtime/rooms/{project_id}")

        assert room_response.status_code == 200
        body = room_response.json()
        assert body == {
            "project_id": project_id,
            "running": False,
            "simcore_tick": 0,
            "rooms": [
                {
                    "room_id": "room_b",
                    "name": sample["rooms"][0]["name"],
                    "floor_id": "F2",
                    "floor_name": "Second floor",
                    "temperature_c": 31.23,
                        "target_temp_c": 24.68,
                        "relative_humidity_pct": 55.6,
                        "target_humidity_pct": 55.0,
                        "airflow_m3_h": 0.0,
                        "cooling_kw": 0.0,
                        "heating_kw": 0.0,
                    },
                {
                    "room_id": "room-2",
                    "name": sample["rooms"][1]["name"],
                    "floor_id": "default_floor",
                    "floor_name": "default_floor",
                    "temperature_c": 31.23,
                        "target_temp_c": 24.68,
                        "relative_humidity_pct": 64.4,
                        "target_humidity_pct": 55.0,
                        "airflow_m3_h": 0.0,
                        "cooling_kw": 0.0,
                        "heating_kw": 0.0,
                    },
            ],
        }
        assert RealtimeRoomStatesResult.model_validate(body).model_dump() == body
        assert client.get(f"/api/v1/projects/{project_id}").json() == project_before
        assert project_path.read_bytes() == project_bytes_before
        assert project_path.stat().st_mtime_ns == project_mtime_before
        assert realtime._sim_configs[project_id] == config_before
        assert client.get(f"/api/v1/realtime/history/{project_id}").json()["points"] == []
        assert not (realtime.HISTORY_DIR / f"{project_id}.json").exists()
        for store in (
            realtime._sessions,
            realtime._layouts,
            realtime._states,
            realtime._updated_at,
            realtime._alarms,
            realtime._summary,
            realtime._subscribers,
            realtime._sim_tasks,
            realtime._control_values,
            realtime._last_floor_states,
            realtime._last_control_state,
        ):
            assert project_id not in store
    finally:
        client.delete(f"/api/v1/projects/{project_id}")


def test_room_states_unknown_project_and_empty_project_contract():
    missing = client.get("/api/v1/realtime/rooms/missing-project")
    assert missing.status_code == 404
    assert missing.json() == {"detail": "Project not found."}

    paths = client.get("/openapi.json").json()["paths"]
    assert paths["/api/v1/realtime/rooms/{project_id}"]["get"]["responses"][
        "404"
    ]["description"] == "Project not found."
    for method in ("get", "post"):
        assert paths["/api/v1/realtime/simulate/config/{project_id}"][method][
            "responses"
        ]["404"]["description"] == "Project not found."

    sample = client.get("/api/v1/project/sample/complete").json()
    sample["rooms"] = []
    response = client.post(
        "/api/v1/projects",
        json={"name": "empty_room_state", "project": sample},
    )
    assert response.status_code == 200
    project_id = response.json()["project_id"]
    try:
        room_response = client.get(f"/api/v1/realtime/rooms/{project_id}")
        assert room_response.status_code == 200
        assert room_response.json() == {
            "project_id": project_id,
            "running": False,
            "simcore_tick": 0,
            "rooms": [],
        }
        assert project_id not in realtime._sessions
        assert project_id not in realtime._layouts
    finally:
        client.delete(f"/api/v1/projects/{project_id}")


def test_stop_unknown_project_returns_not_found():
    response = client.post("/api/v1/realtime/simulate/stop/missing-project")

    assert response.status_code == 404
    assert response.json() == {"detail": "Project not found."}


def test_room_states_follow_authoritative_session_and_config_reset_semantics():
    project_id, _ = _create_project()
    config = {
        "interval_seconds": 10.0,
        "initial_room_temp_c": 30.0,
        "target_temp_c": 20.0,
    }
    try:
        with TestClient(app) as runtime_client:
            try:
                assert runtime_client.post(
                    f"/api/v1/realtime/simulate/config/{project_id}", json=config
                ).status_code == 200
                assert runtime_client.post(
                    f"/api/v1/realtime/simulate/start/{project_id}"
                ).status_code == 200

                advanced = runtime_client.get(
                    f"/api/v1/realtime/rooms/{project_id}"
                ).json()
                assert advanced["running"] is True
                assert advanced["simcore_tick"] == 1
                assert any(
                    room["temperature_c"] != 30.0 for room in advanced["rooms"]
                )

                assert runtime_client.post(
                    f"/api/v1/realtime/simulate/config/{project_id}", json=config
                ).status_code == 200
                unchanged = runtime_client.get(
                    f"/api/v1/realtime/rooms/{project_id}"
                ).json()
                assert unchanged["simcore_tick"] == advanced["simcore_tick"]
                assert unchanged["rooms"] == advanced["rooms"]

                changed_config = {**config, "initial_room_temp_c": 32.0}
                rejected = runtime_client.post(
                    f"/api/v1/realtime/simulate/config/{project_id}",
                    json=changed_config,
                )
                assert rejected.status_code == 409
                assert rejected.json()["detail"]["code"] == (
                    "RUNTIME_CONFIG_REQUIRES_STOP"
                )
                preserved = runtime_client.get(
                    f"/api/v1/realtime/rooms/{project_id}"
                ).json()
                assert preserved == unchanged

                assert runtime_client.post(
                    f"/api/v1/realtime/simulate/stop/{project_id}"
                ).status_code == 200
                assert runtime_client.post(
                    f"/api/v1/realtime/simulate/config/{project_id}",
                    json=changed_config,
                ).status_code == 200
                reset = runtime_client.get(
                    f"/api/v1/realtime/rooms/{project_id}"
                ).json()
                assert reset["running"] is False
                assert reset["simcore_tick"] == 0
                assert {room["temperature_c"] for room in reset["rooms"]} == {32.0}
                assert {room["target_temp_c"] for room in reset["rooms"]} == {20.0}
            finally:
                runtime_client.post(
                    f"/api/v1/realtime/simulate/stop/{project_id}"
                )
    finally:
        client.delete(f"/api/v1/projects/{project_id}")


def test_runtime_config_hot_update_and_stop_start_preserve_session_state():
    project_id, sample = _create_project()
    pump_id = sample["devices"][1]["id"]
    config = {
        "interval_seconds": 10.0,
        "initial_room_temp_c": 30.0,
        "target_temp_c": 24.0,
        "outdoor_base_temp_c": 33.0,
        "outdoor_relative_humidity_pct": 65.0,
    }
    try:
        with TestClient(app) as runtime_client:
            assert runtime_client.post(
                f"/api/v1/realtime/simulate/config/{project_id}", json=config
            ).status_code == 200
            assert runtime_client.post(
                f"/api/v1/realtime/simulate/start/{project_id}"
            ).status_code == 200
            assert runtime_client.post(
                f"/api/v1/realtime/control/{project_id}",
                json={
                    "device_instance_id": pump_id,
                    "values": {"speed_pct": 55.0},
                },
            ).status_code == 200
            session = realtime._sessions[project_id]
            before = session.snapshot
            history_before = len(realtime._read_history(project_id))

            hot = {
                **config,
                "target_temp_c": 22.0,
                "outdoor_base_temp_c": 39.0,
                "outdoor_relative_humidity_pct": 88.0,
                "fault_mode": "terminal_capacity_low",
            }
            accepted = runtime_client.post(
                f"/api/v1/realtime/simulate/config/{project_id}", json=hot
            )
            assert accepted.status_code == 200
            assert realtime._sessions[project_id] is session
            assert session.snapshot == before
            assert len(realtime._read_history(project_id)) == history_before

            state = realtime._simulate_system_tick(
                project_id,
                realtime.get_project(project_id).project,
                session,
                realtime.get_simulation_config(project_id),
            )
            realtime._store_system_state(project_id, state)
            assert session.snapshot.tick == before.tick + 1
            assert state.summary["simcore_tick"] == session.snapshot.tick
            assert state.summary["target_room_temp_c"] == 22.0
            assert state.summary["outdoor_relative_humidity_pct"] == 88.0
            controls = runtime_client.get(
                f"/api/v1/realtime/controls/{project_id}"
            ).json()
            assert controls["devices"][pump_id]["control_source"] == (
                "manual_override"
            )

            assert runtime_client.post(
                f"/api/v1/realtime/simulate/stop/{project_id}"
            ).status_code == 200
            paused_tick = session.snapshot.tick
            paused_rooms = session.snapshot.rooms
            stopped_config = {**hot, "fault_mode": "none", "interval_seconds": 2.0}
            assert runtime_client.post(
                f"/api/v1/realtime/simulate/config/{project_id}",
                json=stopped_config,
            ).status_code == 200
            assert realtime._sessions[project_id] is session
            assert runtime_client.post(
                f"/api/v1/realtime/simulate/start/{project_id}"
            ).status_code == 200
            assert realtime._sessions[project_id] is session
            assert session.snapshot.tick == paused_tick + 1
            assert any(
                session.snapshot.rooms[room_id].temperature_c
                != paused_rooms[room_id].temperature_c
                for room_id in paused_rooms
            )
    finally:
        client.post(f"/api/v1/realtime/simulate/stop/{project_id}")
        client.delete(f"/api/v1/projects/{project_id}")


def test_rated_parameters_and_control_ranges_are_exposed():
    project_id, sample = _create_project()
    try:
        rated = client.get(f"/api/v1/projects/{project_id}/devices/rated")
        assert rated.status_code == 200
        chiller = next(
            item
            for item in rated.json()["devices"]
            if item["device_instance_id"] == "chiller_1"
        )
        assert chiller["model_name"] == "SCWE-90H+B"
        assert chiller["source"] == "catalog"
        assert chiller["values"]["额定容量"] == 308.3

        controls = client.get(f"/api/v1/realtime/controls/{project_id}")
        assert controls.status_code == 200
        pump_id = sample["devices"][1]["id"]
        speed = controls.json()["devices"][pump_id]["controls"]["speed_pct"]
        assert speed == {
            "label": "转速",
            "kind": "number",
            "unit": "%",
            "min": 0.0,
            "max": 100.0,
            "step": 1.0,
            "current": 100.0,
        }
    finally:
        client.delete(f"/api/v1/projects/{project_id}")


def test_rest_control_applies_on_next_tick_and_matches_websocket_state():
    project_id, sample = _create_project()
    pump_id = sample["devices"][1]["id"]
    try:
        response = client.post(
            f"/api/v1/realtime/control/{project_id}",
            json={"device_instance_id": pump_id, "values": {"speed_pct": 50.0}},
        )
        assert response.status_code == 200
        assert (
            response.json()["devices"][pump_id]["controls"]["speed_pct"]["current"]
            == 50.0
        )

        with client.websocket_connect(f"/ws/realtime/{project_id}") as websocket:
            assert websocket.receive_json()["type"] == "connected"
            start = client.post(
                f"/api/v1/realtime/simulate/start/{project_id}",
                json={"interval_seconds": 10.0},
            )
            assert start.status_code == 200
            message = websocket.receive_json()
            while message["type"] != "system_tick":
                message = websocket.receive_json()

            rest_state = client.get(f"/api/v1/realtime/state/{project_id}").json()
            assert message["data"] == rest_state
            assert rest_state["devices"][pump_id]["values"]["speed_pct"] == 50.0
            assert rest_state["devices"][pump_id]["values"]["flow_m3_h"] > 0.0
    finally:
        client.post(f"/api/v1/realtime/simulate/stop/{project_id}")
        client.delete(f"/api/v1/projects/{project_id}")


def test_unknown_runtime_control_is_rejected():
    project_id, sample = _create_project()
    try:
        response = client.post(
            f"/api/v1/realtime/control/{project_id}",
            json={
                "device_instance_id": sample["devices"][0]["id"],
                "values": {"rated_capacity_kw": 1.0},
            },
        )
        assert response.status_code == 422
    finally:
        client.delete(f"/api/v1/projects/{project_id}")


def test_non_finite_runtime_control_is_rejected():
    project_id, sample = _create_project()
    try:
        response = client.post(
            f"/api/v1/realtime/control/{project_id}",
            content=(
                '{"device_instance_id":"'
                + sample["devices"][1]["id"]
                + '","values":{"speed_pct":NaN}}'
            ),
            headers={"content-type": "application/json"},
        )
        assert response.status_code == 422
    finally:
        client.delete(f"/api/v1/projects/{project_id}")


def test_rated_view_reports_project_override_provenance():
    project_id, sample = _create_project()
    try:
        chiller = next(
            device for device in sample["devices"] if device["id"] == "chiller_1"
        )
        chiller["capacity_kw"] = 321.0
        chiller["quantity"] = 3
        client.put(
            f"/api/v1/projects/{project_id}",
            json={"name": "override", "project": sample},
        )

        response = client.get(f"/api/v1/projects/{project_id}/devices/rated")
        assert response.status_code == 200
        view = next(
            item
            for item in response.json()["devices"]
            if item["device_instance_id"] == "chiller_1"
        )
        assert view["values"]["额定容量"] == 321.0
        assert "project_override:capacity_kw" in view["degraded_fields"]

        controls = client.get(f"/api/v1/realtime/controls/{project_id}").json()
        count = controls["devices"]["chiller_1"]["controls"]["active_count"]
        assert (count["min"], count["max"], count["step"], count["current"]) == (
            0.0,
            3.0,
            1.0,
            3.0,
        )
    finally:
        client.delete(f"/api/v1/projects/{project_id}")


def test_heating_realtime_projects_heat_source_hot_loop_and_room_trend():
    project_id, _ = _create_four_floor_project()
    try:
        start = client.post(
            f"/api/v1/realtime/simulate/start/{project_id}",
            json={
                "mode": "heating",
                "interval_seconds": 10.0,
                "initial_room_temp_c": 16.0,
                "target_temp_c": 22.0,
                "outdoor_base_temp_c": 5.0,
            },
        )
        assert start.status_code == 200
        state = client.get(f"/api/v1/realtime/state/{project_id}").json()
        summary = state["summary"]

        assert summary["heating_load_kw"] > 0.0
        assert summary["hot_water_flow_m3_h"] > 0.0
        assert summary["chilled_water_flow_m3_h"] == 0.0
        assert summary["cooling_water_flow_m3_h"] == 0.0
        assert summary["total_flow_m3_h"] == summary["hot_water_flow_m3_h"]
        assert summary["hot_water_supply_temp_c"] > summary["hot_water_return_temp_c"]
        assert summary["delta_t_c"] == pytest.approx(
            summary["supply_water_temp_c"] - summary["return_water_temp_c"],
            abs=0.01,
        )
        assert state["devices"]["ashp_roof"]["values"]["role"] == "heat_source"
        assert state["devices"]["ashp_roof"]["values"]["heating_kw"] > 0.0
        assert state["devices"]["chiller_b1"]["status"] == "stopped"
        assert state["devices"]["hw_pump_b1"]["status"] == "running"

        history = client.get(f"/api/v1/realtime/history/{project_id}").json()
        last_point = history["points"][-1]
        assert last_point["summary"] == summary
        assert last_point["devices"] == state["devices"]
    finally:
        client.post(f"/api/v1/realtime/simulate/stop/{project_id}")
        client.delete(f"/api/v1/projects/{project_id}")


@pytest.mark.parametrize("heat_pump_id", ["ashp_roof"])
def test_cooling_realtime_projects_heat_pump_as_cold_source(heat_pump_id):
    """制冷工况：热泵换向供冷（四通阀教学口径），读数与冷机同构。

    纯热泵系统不再因互锁而无冷源——房间降温、cooling_kw/reject 非零。
    """
    project_id, _ = _create_four_floor_project()
    try:
        # 把样例里的冷机从冷源池里移走，构造"纯热泵制冷"工况。
        response = client.post(
            f"/api/v1/realtime/control/{project_id}",
            json={
                "device_instance_id": "chiller_b1",
                "values": {"enabled": 0.0},
            },
        )
        assert response.status_code == 200
        start = client.post(
            f"/api/v1/realtime/simulate/start/{project_id}",
            json={
                "mode": "cooling",
                "interval_seconds": 10.0,
                "initial_room_temp_c": 30.0,
                "target_temp_c": 26.0,
                "outdoor_base_temp_c": 33.0,
            },
        )
        assert start.status_code == 200
        assert start.json()["running"] is True
        first = client.get(f"/api/v1/realtime/rooms/{project_id}").json()
        _simulate_ticks(project_id, 3)
        second = client.get(f"/api/v1/realtime/rooms/{project_id}").json()

        # TestClient 的后台任务不常驻（与制热测试同口径），用 tick 推进
        # 与温度趋势断言代替 running 标志。
        assert second["simcore_tick"] > first["simcore_tick"]
        # 纯热泵冷源下房间仍应降温（趋势断言，避开瞬态）。
        assert second["rooms"][0]["temperature_c"] < first["rooms"][0][
            "temperature_c"
        ]

        state = client.get(f"/api/v1/realtime/state/{project_id}").json()
        heat = state["devices"][heat_pump_id]["values"]
        assert heat["role"] == "heat_source"
        assert heat["cooling_kw"] > 0.0
        assert heat["heat_reject_kw"] > heat["cooling_kw"]
        assert heat["heating_kw"] == 0.0
        assert 1.5 <= heat["cop"] <= 8.0
        summary = state["summary"]
        assert summary["cooling_load_kw"] > 0.0
        assert summary["installed_cooling_capacity_kw"] > 0.0
    finally:
        client.post(f"/api/v1/realtime/simulate/stop/{project_id}")
        client.delete(f"/api/v1/projects/{project_id}")


def _simulate_ticks(project_id: str, ticks: int) -> None:
    """同步驱动若干 tick（不经墙钟 interval，直接调内部 tick）。"""
    for _ in range(ticks):
        state = realtime._simulate_system_tick(
            project_id,
            realtime.get_project(project_id).project,
            realtime._sessions[project_id],
            realtime.get_simulation_config(project_id),
        )
        realtime._store_system_state(project_id, state)


def test_mixed_chiller_heat_pump_cooling_shares_load():
    """混装制冷：冷机+热泵按容量份额共同分摊，合计覆盖末端负荷。"""
    project_id, _ = _create_four_floor_project()
    try:
        start = client.post(
            f"/api/v1/realtime/simulate/start/{project_id}",
            json={
                "mode": "cooling",
                "interval_seconds": 10.0,
                "initial_room_temp_c": 30.0,
                "target_temp_c": 26.0,
                "outdoor_base_temp_c": 33.0,
            },
        )
        assert start.status_code == 200
        _simulate_ticks(project_id, 3)
        state = client.get(f"/api/v1/realtime/state/{project_id}").json()

        chiller = state["devices"]["chiller_b1"]["values"]
        heat = state["devices"]["ashp_roof"]["values"]
        chiller_kw = chiller.get("cooling_kw", 0.0)
        heat_kw = heat.get("cooling_kw", 0.0)
        # 份额和为 1：两者读数同源分摊，要么都为零（瞬态），要么都>0。
        assert (chiller_kw > 0.0 and heat_kw > 0.0) or (
            chiller_kw == 0.0 and heat_kw == 0.0
        )
        if chiller_kw > 0.0:
            assert state["summary"]["cooling_load_kw"] > 0.0
            # 冷机与热泵份额都小于 1（混装不存在独占者）。
            assert chiller["load_ratio"] < 1.0
            assert heat["load_ratio"] < 1.0
    finally:
        client.post(f"/api/v1/realtime/simulate/stop/{project_id}")
        client.delete(f"/api/v1/projects/{project_id}")


def test_manual_override_reset_and_stop_restart_restore_auto_control():
    project_id, sample = _create_project()
    pump_id = sample["devices"][1]["id"]
    try:
        manual = client.post(
            f"/api/v1/realtime/control/{project_id}",
            json={"device_instance_id": pump_id, "values": {"speed_pct": 50.0}},
        ).json()
        assert manual["devices"][pump_id]["control_source"] == "manual_override"
        assert any(
            device["control_source"] == "auto"
            for device_id, device in manual["devices"].items()
            if device_id != pump_id
        )

        reset = client.post(
            f"/api/v1/realtime/controls/reset/{project_id}",
            json={"device_instance_id": pump_id},
        )
        assert reset.status_code == 200
        assert reset.json()["devices"][pump_id]["control_source"] == "auto"
        assert (
            reset.json()["devices"][pump_id]["controls"]["speed_pct"]["current"]
            == 100.0
        )

        second_device_id = sample["devices"][0]["id"]
        client.post(
            f"/api/v1/realtime/control/{project_id}",
            json={
                "device_instance_id": second_device_id,
                "values": {"enabled": 0.0},
            },
        )
        client.post(
            f"/api/v1/realtime/control/{project_id}",
            json={"device_instance_id": pump_id, "values": {"speed_pct": 55.0}},
        )
        reset_all = client.post(
            f"/api/v1/realtime/controls/reset/{project_id}", json={}
        ).json()
        assert all(
            device["control_source"] == "auto"
            for device in reset_all["devices"].values()
        )
        client.post(
            f"/api/v1/realtime/control/{project_id}",
            json={"device_instance_id": pump_id, "values": {"speed_pct": 55.0}},
        )
        assert client.post(
            f"/api/v1/realtime/simulate/start/{project_id}",
            json={"interval_seconds": 10.0},
        ).status_code == 200
        assert client.post(
            f"/api/v1/realtime/simulate/stop/{project_id}"
        ).status_code == 200
        after_stop = client.get(
            f"/api/v1/realtime/controls/{project_id}"
        ).json()
        assert after_stop["devices"][pump_id]["control_source"] == "auto"
    finally:
        client.post(f"/api/v1/realtime/simulate/stop/{project_id}")
        client.delete(f"/api/v1/projects/{project_id}")


def test_runtime_control_reset_rejects_unknown_device():
    project_id, _ = _create_project()
    try:
        response = client.post(
            f"/api/v1/realtime/controls/reset/{project_id}",
            json={"device_instance_id": "missing-device"},
        )
        assert response.status_code == 404
    finally:
        client.delete(f"/api/v1/projects/{project_id}")


def test_project_edit_rebuilds_layout_from_new_room_geometry():
    project_id, sample = _create_project()
    config = {"interval_seconds": 10.0, "initial_room_temp_c": 30.0}
    try:
        assert client.post(
            f"/api/v1/realtime/simulate/config/{project_id}", json=config
        ).status_code == 200
        assert client.post(
            f"/api/v1/realtime/simulate/start/{project_id}"
        ).status_code == 200
        before = realtime._layouts[project_id].layout.rooms[0].thermal
        assert before.thermal_capacity_kj_k == 9600.0
        assert client.post(
            f"/api/v1/realtime/simulate/stop/{project_id}"
        ).status_code == 200

        enlarged = deepcopy(sample)
        for room in enlarged["rooms"]:
            room["area_m2"] = room["area_m2"] * 2
        assert client.put(
            f"/api/v1/projects/{project_id}",
            json={"name": "enlarged", "project": enlarged},
        ).status_code == 200
        assert project_id not in realtime._sessions
        assert project_id not in realtime._layouts

        assert client.post(
            f"/api/v1/realtime/simulate/start/{project_id}"
        ).status_code == 200
        after = realtime._layouts[project_id].layout.rooms[0].thermal
        # 面积翻倍必须重算热惯量、围护结构导热和照明得热，否则仿真继续用旧布局推进。
        assert after.thermal_capacity_kj_k == before.thermal_capacity_kj_k * 2
        assert after.envelope_conductance_kw_k == pytest.approx(
            before.envelope_conductance_kw_k * 2
        )
        assert after.lighting_w == before.lighting_w * 2
        # 会话重建后从初始温度重新起算，tick 不会延续编辑前的计数。
        assert client.get(f"/api/v1/realtime/state/{project_id}").json()["summary"][
            "simcore_tick"
        ] == 1
    finally:
        client.post(f"/api/v1/realtime/simulate/stop/{project_id}")
        client.delete(f"/api/v1/projects/{project_id}")


def test_project_edit_exposes_added_room_in_runtime_projection():
    project_id, sample = _create_project()
    try:
        assert client.post(
            f"/api/v1/realtime/simulate/start/{project_id}",
            json={"interval_seconds": 10.0},
        ).status_code == 200
        assert client.post(
            f"/api/v1/realtime/simulate/stop/{project_id}"
        ).status_code == 200

        grown = deepcopy(sample)
        added = deepcopy(grown["rooms"][0])
        added.update({"id": "lab_1", "name": "lab_1"})
        grown["rooms"].append(added)
        assert client.put(
            f"/api/v1/projects/{project_id}", json={"name": "grown", "project": grown}
        ).status_code == 200

        # 旧快照没有新房间的读数，不清缓存这里会以 KeyError('lab_1') 崩掉投影。
        rooms = client.get(f"/api/v1/realtime/rooms/{project_id}")
        assert rooms.status_code == 200
        assert [room["room_id"] for room in rooms.json()["rooms"]] == [
            "office_1",
            "meeting_1",
            "lab_1",
        ]
    finally:
        client.delete(f"/api/v1/projects/{project_id}")


def test_project_edit_prunes_overrides_of_removed_devices_only():
    project_id, sample = _create_project()
    pump_id = sample["devices"][1]["id"]
    try:
        for device_instance_id, values in (
            ("tower_1", {"fan_speed_pct": 40.0}),
            (pump_id, {"speed_pct": 44.0}),
        ):
            assert client.post(
                f"/api/v1/realtime/control/{project_id}",
                json={"device_instance_id": device_instance_id, "values": values},
            ).status_code == 200

        trimmed = deepcopy(sample)
        trimmed["devices"] = [
            device for device in trimmed["devices"] if device["id"] != "tower_1"
        ]
        trimmed["pipes"] = [
            pipe for pipe in trimmed["pipes"] if not pipe["id"].startswith("cw_")
        ]
        assert client.put(
            f"/api/v1/projects/{project_id}",
            json={"name": "trimmed", "project": trimmed},
        ).status_code == 200

        # 只丢掉已删除设备的手动值；留着它会让 _ensure_session 重放时抛 KeyError。
        assert realtime._control_values[project_id] == {pump_id: {"speed_pct": 44.0}}
        controls = client.get(f"/api/v1/realtime/controls/{project_id}")
        assert controls.status_code == 200
        devices = controls.json()["devices"]
        assert "tower_1" not in devices
        assert devices[pump_id]["controls"]["speed_pct"]["current"] == 44.0
        assert devices[pump_id]["control_source"] == "manual_override"
    finally:
        client.delete(f"/api/v1/projects/{project_id}")


def test_project_edit_prunes_initial_conditions_of_removed_rooms():
    project_id, sample = _create_project()
    try:
        assert client.post(
            f"/api/v1/realtime/simulate/config/{project_id}",
            json={
                "room_initial_conditions": [
                    {
                        "room_id": "meeting_1",
                        "temperature_c": 27.0,
                        "relative_humidity_pct": 62.0,
                    }
                ]
            },
        ).status_code == 200

        trimmed = deepcopy(sample)
        trimmed["rooms"] = [
            room for room in trimmed["rooms"] if room["id"] != "meeting_1"
        ]
        trimmed["devices"] = [
            device for device in trimmed["devices"] if device["id"] != "fcu_1"
        ]
        # 删设备要同步摘掉房间侧引用，否则会被 ROOM_BINDING_UNKNOWN 拒掉。
        for room in trimmed["rooms"]:
            room["device_ids"] = [
                device_id for device_id in room.get("device_ids", []) if device_id != "fcu_1"
            ]
        assert client.put(
            f"/api/v1/projects/{project_id}",
            json={"name": "trimmed", "project": trimmed},
        ).status_code == 200

        # 留下已删除房间的初始条件，SimulationSession 会拒绝未知 room id。
        assert client.get(
            f"/api/v1/realtime/simulate/config/{project_id}"
        ).json()["room_initial_conditions"] == []
        rooms = client.get(f"/api/v1/realtime/rooms/{project_id}")
        assert rooms.status_code == 200
        assert [room["room_id"] for room in rooms.json()["rooms"]] == ["office_1"]
        assert client.post(
            f"/api/v1/realtime/simulate/start/{project_id}",
            json={"interval_seconds": 10.0},
        ).status_code == 200
    finally:
        client.post(f"/api/v1/realtime/simulate/stop/{project_id}")
        client.delete(f"/api/v1/projects/{project_id}")


def test_project_edit_drops_stale_updated_at_with_device_snapshot():
    project_id, sample = _create_project()
    try:
        assert client.post(
            f"/api/v1/realtime/simulate/start/{project_id}",
            json={"interval_seconds": 10.0},
        ).status_code == 200
        assert client.post(
            f"/api/v1/realtime/simulate/stop/{project_id}"
        ).status_code == 200
        before = client.get(f"/api/v1/realtime/state/{project_id}").json()
        assert before["updated_at"] is not None
        assert before["devices"]

        assert client.put(
            f"/api/v1/projects/{project_id}", json={"name": "edited", "project": sample}
        ).status_code == 200

        # 设备快照清了就不能留时间戳，否则前端把空快照当成刚刷新的数据。
        after = client.get(f"/api/v1/realtime/state/{project_id}").json()
        assert after["devices"] == {}
        assert after["updated_at"] is None
    finally:
        client.delete(f"/api/v1/projects/{project_id}")


def test_project_edit_is_rejected_while_simulation_runs():
    project_id, sample = _create_project()
    config = {
        "interval_seconds": 10.0,
        "initial_room_temp_c": 30.0,
        "target_temp_c": 24.0,
    }
    edited = deepcopy(sample)
    for room in edited["rooms"]:
        room["area_m2"] = room["area_m2"] * 2
    try:
        with TestClient(app) as runtime_client:
            try:
                assert runtime_client.post(
                    f"/api/v1/realtime/simulate/config/{project_id}", json=config
                ).status_code == 200
                assert runtime_client.post(
                    f"/api/v1/realtime/simulate/start/{project_id}"
                ).status_code == 200
                advanced = runtime_client.get(
                    f"/api/v1/realtime/rooms/{project_id}"
                ).json()
                assert advanced["running"] is True
                assert advanced["simcore_tick"] == 1

                # 编辑会重建 session，运行中静默重建会把 tick 打回 0 并丢掉轨迹，
                # 与改 mode 时的 409 口径保持一致。
                rejected = runtime_client.put(
                    f"/api/v1/projects/{project_id}",
                    json={"name": "pytest_realtime", "project": edited},
                )
                assert rejected.status_code == 409
                assert rejected.json()["detail"]["code"] == "PROJECT_EDIT_REQUIRES_STOP"
                assert runtime_client.get("/openapi.json").json()["paths"][
                    "/api/v1/projects/{project_id}"
                ]["put"]["responses"]["409"]["description"] == (
                    "Stop the simulation before editing the project."
                )

                # 拒绝必须彻底：仿真不能被重置，改动也不能已经落盘。
                assert runtime_client.get(
                    f"/api/v1/realtime/rooms/{project_id}"
                ).json() == advanced
                stored = runtime_client.get(f"/api/v1/projects/{project_id}").json()
                assert stored["project"]["rooms"][0]["area_m2"] == (
                    sample["rooms"][0]["area_m2"]
                )

                # 停机后同一次编辑必须被接受，并按新布局重建。
                assert runtime_client.post(
                    f"/api/v1/realtime/simulate/stop/{project_id}"
                ).status_code == 200
                assert runtime_client.put(
                    f"/api/v1/projects/{project_id}",
                    json={"name": "pytest_realtime", "project": edited},
                ).status_code == 200
                assert project_id not in realtime._sessions
                reset = runtime_client.get(
                    f"/api/v1/realtime/rooms/{project_id}"
                ).json()
                assert reset["running"] is False
                assert reset["simcore_tick"] == 0
            finally:
                runtime_client.post(f"/api/v1/realtime/simulate/stop/{project_id}")
    finally:
        client.delete(f"/api/v1/projects/{project_id}")


def test_project_edit_keeps_websocket_subscribers_connected():
    project_id, sample = _create_project()
    try:
        with client.websocket_connect(f"/ws/realtime/{project_id}") as websocket:
            assert websocket.receive_json()["type"] == "connected"

            enlarged = deepcopy(sample)
            for room in enlarged["rooms"]:
                room["area_m2"] = room["area_m2"] * 2
            assert client.put(
                f"/api/v1/projects/{project_id}",
                json={"name": "enlarged", "project": enlarged},
            ).status_code == 200

            # 编辑不是停机操作，不该踢掉已连接的前端。
            assert realtime._subscribers[project_id]
            assert project_id not in realtime._sessions
    finally:
        client.delete(f"/api/v1/projects/{project_id}")


def test_realtime_device_states_lists_every_device_and_filters_by_type():
    project_id, _ = _create_four_floor_project()
    try:
        # 未启动仿真时也应返回 tick-zero 只读快照（不创建 session 历史）
        body = client.get(f"/api/v1/realtime/devices/{project_id}").json()
        assert body["running"] is False
        assert body["simcore_tick"] == 0
        assert body["device_count"] == len(body["devices"]) > 0
        assert body["filtered_by_type"] is None

        first = body["devices"][0]
        for field in ("device_id", "device_type", "role", "status", "values", "parameter_origin"):
            assert field in first

        chillers = client.get(
            f"/api/v1/realtime/devices/{project_id}", params={"type": "冷源机组"}
        ).json()
        assert chillers["filtered_by_type"] == "冷源机组"
        assert chillers["device_count"] >= 1
        assert {row["device_type"] for row in chillers["devices"]} == {"冷源机组"}

        fans = client.get(
            f"/api/v1/realtime/devices/{project_id}", params={"type": "风机盘管"}
        ).json()
        assert all(row["device_type"] == "风机盘管" for row in fans["devices"])
    finally:
        client.delete(f"/api/v1/projects/{project_id}")


def test_realtime_device_states_single_device_and_error_codes():
    project_id, sample = _create_four_floor_project()
    try:
        device_id = sample["devices"][0]["id"]
        body = client.get(f"/api/v1/realtime/devices/{project_id}/{device_id}").json()
        assert body["device_count"] == 1
        assert body["devices"][0]["device_id"] == device_id

        assert client.get(
            f"/api/v1/realtime/devices/{project_id}/not_a_device"
        ).status_code == 404
        assert client.get(
            f"/api/v1/realtime/devices/{project_id}", params={"type": "不存在的设备类型"}
        ).status_code == 422
        assert client.get(
            "/api/v1/realtime/devices/not_a_project"
        ).status_code == 404
    finally:
        client.delete(f"/api/v1/projects/{project_id}")


def test_device_state_updated_pushes_only_changed_devices():
    """增量契约：device_state_updated 只携带本次变化的设备，客户端负责合并。"""
    project_id, sample = _create_project()
    try:
        target = sample["devices"][0]["id"]
        with client.websocket_connect(f"/ws/realtime/{project_id}") as websocket:
            assert websocket.receive_json()["type"] == "connected"

            client.post(
                "/api/v1/realtime/update",
                json={
                    "project_id": project_id,
                    "states": [
                        {
                            "project_id": project_id,
                            "device_id": target,
                            "status": "running",
                            "values": {"load_ratio": 0.5},
                        }
                    ],
                },
            )
            message = websocket.receive_json()
            assert message["type"] == "device_state_updated"
            # 只推变化的那一台，而非整项目的全量设备
            assert list(message["data"]["devices"]) == [target]
            assert message["data"]["project_id"] == project_id
    finally:
        client.delete(f"/api/v1/projects/{project_id}")
