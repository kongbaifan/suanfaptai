"""Backend acceptance checks for the two clean teaching project samples.

The browser flow ultimately uses the same ``POST /api/v1/projects`` contract,
so this test keeps the persistence/runtime assertions at the API boundary.  A
temporary project directory is used deliberately: the repository's existing
project JSON files are fixtures owned by the user and must remain untouched.
"""

from __future__ import annotations

import hashlib
import math
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import realtime
from app.data import project_store
from app.main import app
from app.schemas import SystemEvaluateRequest


SAMPLE_NAMES = ("four_floor_fcu_fresh_air", "multi_zone_all_air")
TERMINAL_TYPES = frozenset({"风机盘管", "组合式空调机组", "新风机组"})
SYSTEM_EQUIPMENT_TYPES = frozenset(
    {
        "冷源机组",
        "冷水机组",
        "循环泵",
        "冷却塔",
        "空气源热泵",
        "水源热泵",
        "地源热泵",
        "地源热泵机组",
    }
)


@pytest.fixture
def api_client():
    """Keep the TestClient lifespan alive so the realtime task can tick."""
    with TestClient(app) as client:
        yield client


def _project_file_snapshot() -> dict[str, tuple[str, int]]:
    """Return filename -> (SHA-256, mtime_ns) for the real project fixtures."""
    project_dir = Path(__file__).resolve().parents[1] / "app" / "data" / "projects"
    return {
        path.name: (
            hashlib.sha256(path.read_bytes()).hexdigest(),
            path.stat().st_mtime_ns,
        )
        for path in sorted(project_dir.glob("*.json"))
    }


def _sample(client: TestClient, name: str) -> dict[str, Any]:
    response = client.get(f"/api/v1/project/sample/{name}")
    assert response.status_code == 200, response.text
    return response.json()


def _save_sample(client: TestClient, name: str, sample: dict[str, Any]) -> dict[str, Any]:
    response = client.post(
        "/api/v1/projects",
        json={"name": f"课堂-{name}", "project": sample},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _assert_clean_structure(sample: dict[str, Any], loaded: dict[str, Any]) -> None:
    # Pydantic fills omitted defaults and normalizes integer-valued floats on
    # save, so compare the round-trip against the canonical DTO dump rather
    # than the raw sample JSON representation.
    assert loaded == SystemEvaluateRequest.model_validate(sample).model_dump()
    devices_by_id = {device["id"]: device for device in loaded["devices"]}
    for room in loaded["rooms"]:
        for device_id in room.get("device_ids", []):
            assert devices_by_id[device_id]["type"] in TERMINAL_TYPES

    # The service relationship belongs to rooms; legacy device-side bindings
    # must not reappear in a newly saved project.
    assert all("served_room_ids" not in device for device in loaded["devices"])

    # Placement is an independent, persisted relationship for system equipment.
    for device in loaded["devices"]:
        if device["type"] not in SYSTEM_EQUIPMENT_TYPES:
            continue
        assert device.get("floor_id")
        assert device.get("location_type") in {"equipment_room", "roof"}


@pytest.mark.parametrize("sample_name", SAMPLE_NAMES)
def test_clean_sample_round_trip_validate_and_runtime(
    sample_name: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    api_client: TestClient,
):
    old_project_files = _project_file_snapshot()
    monkeypatch.setattr(project_store, "PROJECT_DIR", tmp_path / "projects")
    monkeypatch.setattr(realtime, "HISTORY_DIR", tmp_path / "history")
    client = api_client
    sample = _sample(client, sample_name)
    record = _save_sample(client, sample_name, deepcopy(sample))
    project_id = record["project_id"]

    try:
        assert record["project"] == SystemEvaluateRequest.model_validate(
            sample
        ).model_dump()
        loaded_response = client.get(f"/api/v1/projects/{project_id}")
        assert loaded_response.status_code == 200
        loaded = loaded_response.json()["project"]
        _assert_clean_structure(sample, loaded)
        if sample_name == "four_floor_fcu_fresh_air":
            assert len(loaded["rooms"]) == 6
            assert all(
                set(room["device_ids"]) == {f"fcu_f{floor}", f"fau_f{floor}"}
                for floor, room_group in ((1, loaded["rooms"][0:2]),
                                          (2, loaded["rooms"][2:4]),
                                          (3, loaded["rooms"][4:6]))
                for room in room_group
            )
            expected_locations = {
                "chiller_b1": ("B1F", "equipment_room"),
                "chw_pump_b1": ("B1F", "equipment_room"),
                "cw_pump_b1": ("B1F", "equipment_room"),
                "hw_pump_b1": ("B1F", "equipment_room"),
                "tower_roof": ("F3", "roof"),
                "ashp_roof": ("F3", "roof"),
            }
        else:
            assert [room["device_ids"] for room in loaded["rooms"]] == [
                ["ahu_f1"],
                ["ahu_f1"],
                ["ahu_f1"],
            ]
            expected_locations = {
                "chiller_b1": ("B1F", "equipment_room"),
                "chw_pump_b1": ("B1F", "equipment_room"),
                "cw_pump_b1": ("B1F", "equipment_room"),
                "hw_pump_b1": ("B1F", "equipment_room"),
                "tower_rf": ("RF", "roof"),
                "ashp_rf": ("RF", "roof"),
            }
        devices_by_id = {device["id"]: device for device in loaded["devices"]}
        assert {
            device_id: (devices_by_id[device_id].get("floor_id"), devices_by_id[device_id].get("location_type"))
            for device_id in expected_locations
        } == expected_locations
        assert project_id not in realtime._sessions
        assert project_id not in realtime._sim_tasks

        validation = client.post("/api/v1/project/validate", json=loaded)
        assert validation.status_code == 200, validation.text
        validation_body = validation.json()
        assert validation_body["valid"] is True
        binding_codes = {
            issue["code"]
            for issue in validation_body["issues"]
            if issue["code"].startswith("ROOM_BINDING_")
        }
        assert binding_codes.isdisjoint(
            {"ROOM_BINDING_NO_DEVICE", "ROOM_BINDING_DEVICE_UNCLAIMED"}
        )

        # Start only after the structural validation.  The first tick is
        # published synchronously by the API; the second one proves that room
        # temperature/humidity and terminal delivery are dynamic states.
        with client.websocket_connect(f"/ws/realtime/{project_id}") as websocket:
            assert websocket.receive_json()["type"] == "connected"
            started = client.post(
                f"/api/v1/realtime/simulate/start/{project_id}",
                json={
                    "mode": "cooling",
                    "interval_seconds": 1.0,
                    "initial_room_temp_c": 30.0,
                    "target_temp_c": 26.0,
                },
            )
            assert started.status_code == 200, started.text

            first_message = websocket.receive_json()
            while first_message["type"] != "system_tick":
                first_message = websocket.receive_json()
            first_state = first_message["data"]
            first_room_response = client.get(
                f"/api/v1/realtime/rooms/{project_id}"
            )
            assert first_room_response.status_code == 200
            first_rooms = first_room_response.json()
            assert first_rooms["simcore_tick"] == first_state["summary"][
                "simcore_tick"
            ]

            second_message = websocket.receive_json()
            while second_message["type"] != "system_tick":
                second_message = websocket.receive_json()
            second_state = second_message["data"]

            rest_state_response = client.get(
                f"/api/v1/realtime/state/{project_id}"
            )
            assert rest_state_response.status_code == 200
            rest_state = rest_state_response.json()
            assert rest_state == second_state
            current_room_response = client.get(
                f"/api/v1/realtime/rooms/{project_id}"
            )
            assert current_room_response.status_code == 200
            current_rooms = current_room_response.json()

        assert current_rooms["simcore_tick"] == rest_state["summary"]["simcore_tick"]
        assert current_rooms["running"] is True
        assert current_rooms["rooms"]
        assert all(
            math.isfinite(room[field])
            for room in current_rooms["rooms"]
            for field in (
                "temperature_c",
                "relative_humidity_pct",
                "airflow_m3_h",
                "cooling_kw",
            )
        )
        first_by_id = {room["room_id"]: room for room in first_rooms["rooms"]}
        assert any(
            room["temperature_c"] != first_by_id[room["room_id"]]["temperature_c"]
            for room in current_rooms["rooms"]
        )
        assert any(
            room["relative_humidity_pct"]
            != first_by_id[room["room_id"]]["relative_humidity_pct"]
            for room in current_rooms["rooms"]
        )
        assert any(
            device_state["values"].get("water_flow_m3_h", 0.0) > 0.0
            or device_state["values"].get("airflow_m3_h", 0.0) > 0.0
            for device_state in rest_state["devices"].values()
            if device_state["values"].get("role") == "terminal"
        )
        assert {
            issue["code"] for issue in rest_state["alarms"]
        }.isdisjoint(
            {"ROOM_BINDING_NO_DEVICE", "ROOM_BINDING_DEVICE_UNCLAIMED"}
        )

        history = client.get(f"/api/v1/realtime/history/{project_id}")
        assert history.status_code == 200
        assert history.json()["points"][-1]["devices"] == rest_state["devices"]
    finally:
        client.post(f"/api/v1/realtime/simulate/stop/{project_id}")
        client.delete(f"/api/v1/projects/{project_id}")

    assert _project_file_snapshot() == old_project_files


def test_two_clean_samples_create_independent_project_records(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    api_client: TestClient,
):
    old_project_files = _project_file_snapshot()
    monkeypatch.setattr(project_store, "PROJECT_DIR", tmp_path / "projects")
    monkeypatch.setattr(realtime, "HISTORY_DIR", tmp_path / "history")
    client = api_client
    records = [
        _save_sample(client, name, _sample(client, name)) for name in SAMPLE_NAMES
    ]
    ids = [record["project_id"] for record in records]
    assert len(set(ids)) == len(SAMPLE_NAMES)
    listed = client.get("/api/v1/projects")
    assert listed.status_code == 200
    assert {item["project_id"] for item in listed.json()} == set(ids)
    for project_id in ids:
        assert client.get(f"/api/v1/projects/{project_id}").status_code == 200
        client.delete(f"/api/v1/projects/{project_id}")
    assert _project_file_snapshot() == old_project_files
