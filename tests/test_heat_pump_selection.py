from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import equipment_selection, realtime
from app.data import project_store
from app.data.equipment import (
    CATALOG_PATH,
    capacity_for_device,
    find_equipment_spec,
    flow_for_device,
    load_equipment_catalog,
    role_for_device,
    systems_for_device,
)
from app.equipment_selection import recommend_equipment
from app.main import app
from app.samples import COMPLETE_SYSTEM_SAMPLE
from app.schemas import DeviceInput, SelectionRequirements, SystemEvaluateRequest
from app.steady_solver.solver import solve_steady_state


HEAT_PUMP_TYPES = ("空气源热泵", "水源热泵", "地源热泵机组")


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(project_store, "PROJECT_DIR", tmp_path / "projects")
    monkeypatch.setattr(realtime, "HISTORY_DIR", tmp_path / "realtime_history")
    yield TestClient(app)


def _requirements(
    *, heating_load_kw: float = 100.0, hot_water_flow_m3_h: float = 25.0
) -> SelectionRequirements:
    return SelectionRequirements(
        cooling_load_kw=100.0,
        heating_load_kw=heating_load_kw,
        chilled_water_flow_m3_h=17.2,
        hot_water_flow_m3_h=hot_water_flow_m3_h,
        required_pump_head_m=20.0,
        supply_airflow_m3_h=10_000.0,
    )


def _stub_selection(
    monkeypatch: pytest.MonkeyPatch,
    rows: list[dict],
    requirements: SelectionRequirements | None = None,
) -> None:
    record = SimpleNamespace(project=SimpleNamespace(rooms=[object()]))
    monkeypatch.setattr(equipment_selection, "get_project", lambda _id: record)
    monkeypatch.setattr(
        equipment_selection,
        "_derive_requirements",
        lambda _record: requirements or _requirements(),
    )
    monkeypatch.setattr(equipment_selection, "load_equipment_catalog", lambda: rows)


def _saved_load_only_project(client: TestClient) -> str:
    project = deepcopy(COMPLETE_SYSTEM_SAMPLE)
    project["devices"] = []
    project["pipes"] = []
    project["connections"] = []
    # 设备清空后房间的 device_ids 会指向不存在的设备，落盘会被 ROOM_BINDING_UNKNOWN 拒掉。
    for room in project["rooms"]:
        room["device_ids"] = []
    response = client.post(
        "/api/v1/projects", json={"name": "heat-source", "project": project}
    )
    assert response.status_code == 200, response.text
    return response.json()["project_id"]


def test_catalog_has_three_formal_models_for_each_heat_pump_type():
    catalog = load_equipment_catalog()

    for device_type in HEAT_PUMP_TYPES:
        rows = [row for row in catalog if row.get("type") == device_type]
        assert len(rows) == 3
        assert all(row["name"] != "*" for row in rows)
        assert all(row["role"] == "heat_source" for row in rows)
        assert all(row["systems"] == ["chilled_water", "hot_water"] for row in rows)
        assert all(
            isinstance(row[field], (int, float)) and row[field] > 0
            for row in rows
            for field in (
                "capacity_kw",
                "flow_m3_h",
                "power_kw",
                "cop",
                "cooling_capacity_kw",
                "cooling_power_kw",
                "eer",
            )
        )

        exact = find_equipment_spec(device_type, rows[1]["name"])
        fallback = find_equipment_spec(device_type, "unknown-model")
        assert exact == rows[1]
        assert fallback is None

    assert find_equipment_spec("空气源热泵", "ASHP-060")["name"] == "CGK-080V4-B"
    assert find_equipment_spec("水源热泵", "WSHP-080")["name"] == "CGS-42(HC)"
    assert find_equipment_spec("地源热泵机组", "GSHP-100")["name"] == "W-300-HAC"


def test_ground_source_heat_pumps_use_manual_indoor_and_outdoor_loop_flows():
    rows = [
        row
        for row in load_equipment_catalog()
        if row.get("type") == "地源热泵机组"
    ]

    assert [
        (row["name"], row["flow_m3_h"], row["source_flow_m3_h"])
        for row in rows
    ] == [
        ("W-300-HAC", 16.2, 16.2),
        ("W-500-HAC", 27.36, 27.36),
        ("W-800-HAC", 43.2, 43.2),
    ]
    assert all("002746MAN-01" in row["source_url"] for row in rows)


def test_catalog_heat_pumps_include_operating_water_temperatures():
    expected = {
        "空气源热泵": {
            "cooling_evaporator_inlet_temp_c": 12.0,
            "cooling_evaporator_outlet_temp_c": 7.0,
            "heating_condenser_inlet_temp_c": 30.0,
            "heating_condenser_outlet_temp_c": 35.0,
        },
        "水源热泵": {
            "heating_evaporator_inlet_temp_c": 15.0,
            "heating_evaporator_outlet_temp_c": 10.0,
            "heating_condenser_inlet_temp_c": 15.0,
            "heating_condenser_outlet_temp_c": 55.0,
        },
        "地源热泵机组": {
            "cooling_evaporator_inlet_temp_c": 12.0,
            "cooling_condenser_inlet_temp_c": 25.0,
            "heating_evaporator_inlet_temp_c": 0.0,
            "heating_condenser_inlet_temp_c": 40.0,
        },
    }

    for device_type, temperatures in expected.items():
        rows = [
            row for row in load_equipment_catalog() if row.get("type") == device_type
        ]
        assert all(
            row.get(field) == value
            for row in rows
            for field, value in temperatures.items()
        )

    water_rows = [
        row for row in load_equipment_catalog() if row.get("type") == "水源热泵"
    ]
    ground_rows = [
        row for row in load_equipment_catalog() if row.get("type") == "地源热泵机组"
    ]
    assert all("cooling_evaporator_inlet_temp_c" not in row for row in water_rows)
    assert all("cooling_condenser_outlet_temp_c" not in row for row in water_rows)
    assert all("cooling_evaporator_outlet_temp_c" not in row for row in ground_rows)
    assert all("heating_condenser_outlet_temp_c" not in row for row in ground_rows)


def test_catalog_heat_pumps_include_cooling_performance_and_air_conditions():
    expected = {
        "空气源热泵": [
            ("CGK-080V4-B", 17.0, 6.641, 2.56),
            ("CGK-100V4-B", 19.2, 7.273, 2.64),
            ("CGK-100V3L-B", 20.8, 8.0, 2.6),
        ],
        "水源热泵": [
            ("CGS-42(HC)", 39.1, 8.58, 4.56),
            ("CGS-72(HC)", 64.6, 14.2, 4.55),
            ("CGS-95(HC)", 85.0, 18.72, 4.54),
        ],
        "地源热泵机组": [
            ("W-300-HAC", 77.0, 16.18, 4.75),
            ("W-500-HAC", 133.0, 28.287, 4.69),
            ("W-800-HAC", 210.0, 45.562, 4.6),
        ],
    }
    catalog = load_equipment_catalog()

    for device_type, rows in expected.items():
        actual = [row for row in catalog if row.get("type") == device_type]
        assert [
            (
                row["name"],
                row["cooling_capacity_kw"],
                row["cooling_power_kw"],
                row["eer"],
            )
            for row in actual
        ] == rows

    air_source = [row for row in catalog if row.get("type") == "空气源热泵"]
    assert all(row["cooling_outdoor_dry_bulb_temp_c"] == 35.0 for row in air_source)
    assert all(row["heating_outdoor_dry_bulb_temp_c"] == 7.0 for row in air_source)


def test_catalog_heat_pump_helpers_expose_role_system_capacity_and_flow():
    device = DeviceInput(
        id="heat-pump", type="空气源热泵", name="CGK-080V4-B", quantity=2
    )

    assert role_for_device(device) == "heat_source"
    assert systems_for_device(device) == {"chilled_water", "hot_water"}
    assert capacity_for_device(device) == 50.0
    assert flow_for_device(device) == 8.6


def test_heat_source_quantity_covers_capacity_and_hot_water_flow(
    monkeypatch: pytest.MonkeyPatch,
):
    _stub_selection(
        monkeypatch,
        [
            {
                "type": "空气源热泵",
                "name": "FLOW-LIMITED",
                "role": "heat_source",
                "capacity_kw": 60.0,
                "flow_m3_h": 10.0,
                "power_kw": 18.0,
                "cop": 3.3,
            }
        ],
    )

    candidate = recommend_equipment("project", "heat_source").candidates[0]

    assert candidate.quantity == 3
    assert candidate.rated.capacity_kw == 60.0
    assert candidate.rated.flow_m3_h == 10.0
    assert candidate.rated.cop == 3.3
    assert candidate.provided.capacity_kw == 180.0
    assert candidate.provided.flow_m3_h == 30.0
    assert candidate.provided.power_kw == 54.0
    assert candidate.margins.hot_water_flow == 1.2
    assert candidate.eligible is True


def test_heat_source_missing_capacity_or_flow_blocks_but_display_fields_do_not(
    monkeypatch: pytest.MonkeyPatch,
):
    _stub_selection(
        monkeypatch,
        [
            {
                "type": "水源热泵",
                "name": "NO-DISPLAY-FIELDS",
                "role": "heat_source",
                "capacity_kw": 100.0,
                "flow_m3_h": 25.0,
            },
            {
                "type": "水源热泵",
                "name": "MALFORMED",
                "role": "heat_source",
                "capacity_kw": float("nan"),
                "flow_m3_h": 0.0,
                "power_kw": True,
                "cop": -1.0,
            },
        ],
    )

    result = recommend_equipment("project", "heat_source")
    candidates = {item.model_name: item for item in result.candidates}

    assert result.status == "recommended"
    assert candidates["NO-DISPLAY-FIELDS"].eligible is True
    assert candidates["NO-DISPLAY-FIELDS"].rated.power_kw is None
    assert candidates["NO-DISPLAY-FIELDS"].rated.cop is None
    assert candidates["MALFORMED"].eligible is False
    assert candidates["MALFORMED"].rated.capacity_kw is None
    assert candidates["MALFORMED"].rated.flow_m3_h is None
    assert [reason.code for reason in candidates["MALFORMED"].reasons] == [
        "MISSING_RATED_PARAMETER",
        "MISSING_RATED_PARAMETER",
    ]


def test_heat_source_statuses_and_order_are_deterministic(
    monkeypatch: pytest.MonkeyPatch,
):
    _stub_selection(monkeypatch, [])
    assert recommend_equipment("project", "heat_source").status == (
        "no_catalog_candidates"
    )

    rows = [
        {
            "type": device_type,
            "name": "INCOMPLETE",
            "role": "heat_source",
            "capacity_kw": 100.0,
        }
        for device_type in reversed(HEAT_PUMP_TYPES)
    ]
    monkeypatch.setattr(equipment_selection, "load_equipment_catalog", lambda: rows)

    first = recommend_equipment("project", "heat_source")
    second = recommend_equipment("project", "heat_source")

    assert first == second
    assert first.status == "no_eligible_candidates"
    assert [item.device_type for item in first.candidates] == sorted(HEAT_PUMP_TYPES)


def test_heat_source_api_derives_heating_and_hot_water_requirements_read_only(
    client: TestClient,
):
    project_id = _saved_load_only_project(client)
    project_path = project_store.PROJECT_DIR / f"{project_id}.json"
    project_before = project_path.read_bytes()
    catalog_before = CATALOG_PATH.read_bytes()

    first = client.post(
        f"/api/v1/selection/recommend/{project_id}",
        json={"target_role": "heat_source"},
    )
    second = client.post(
        f"/api/v1/selection/recommend/{project_id}",
        json={"target_role": "heat_source"},
    )

    assert first.status_code == 200, first.text
    assert first.json() == second.json()
    body = first.json()
    assert body["status"] == "recommended"
    assert body["requirements"]["heating_load_kw"] > 0
    assert body["requirements"]["hot_water_flow_m3_h"] > 0
    assert len(body["candidates"]) == 9
    assert all(item["role"] == "heat_source" for item in body["candidates"])
    assert all(item["eligible"] for item in body["candidates"])
    assert project_path.read_bytes() == project_before
    assert CATALOG_PATH.read_bytes() == catalog_before
    assert project_store.get_project(project_id).history == []
    assert project_id not in realtime._sessions


def test_heat_source_api_reports_nonpositive_derived_requirements(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
):
    project_id = _saved_load_only_project(client)
    monkeypatch.setattr(
        equipment_selection,
        "calculate_load",
        lambda _request: SimpleNamespace(
            adjusted_cooling_kw=1.0, adjusted_heating_kw=0.0
        ),
    )

    response = client.post(
        f"/api/v1/selection/recommend/{project_id}",
        json={"target_role": "heat_source"},
    )

    assert response.status_code == 422
    assert response.json()["detail"] == {
        "code": "SELECTION_REQUIREMENTS_UNAVAILABLE",
        "target_role": "heat_source",
        "missing": ["heating_load_kw", "hot_water_flow_m3_h"],
        "message": "Required project data is unavailable for equipment selection.",
    }


def test_openapi_exposes_heat_source_role_and_additive_requirements():
    openapi = app.openapi()
    schema = openapi["components"]["schemas"]

    assert "heat_source" in schema["SelectionRecommendationRequest"]["properties"][
        "target_role"
    ]["enum"]
    requirement_fields = schema["SelectionRequirements"]["properties"]
    assert "heating_load_kw" in requirement_fields
    assert "hot_water_flow_m3_h" in requirement_fields
    responses = openapi["paths"]["/api/v1/selection/recommend/{project_id}"][
        "post"
    ]["responses"]
    assert responses["404"]["description"] == "Project not found."
    assert "422" in responses


def test_catalog_heat_pump_runs_through_existing_hot_water_solver():
    request = SystemEvaluateRequest(
        devices=[
            DeviceInput(
                id="heat-source", type="地源热泵机组", name="W-500-HAC"
            ),
            DeviceInput(id="pump", type="循环泵", flow_m3_h=20.0, head_m=20.0),
            DeviceInput(id="terminal", type="terminal", capacity_kw=80.0),
        ],
        connections=[
            {"from_id": "heat-source", "to_id": "pump", "system": "hot_water"},
            {"from_id": "pump", "to_id": "terminal", "system": "hot_water"},
            {
                "from_id": "terminal",
                "to_id": "heat-source",
                "system": "hot_water",
            },
        ],
    )

    result = solve_steady_state(
        request, cooling_load_kw=0.0, heating_load_kw=80.0
    )

    assert "hot_water" in result.flow_allocations
    assert result.equipment_balance is not None
    assert result.equipment_balance.heat_source_capacity_kw == 108.0
    assert result.equipment_balance.heating_balance_residual_kw <= 0.01
