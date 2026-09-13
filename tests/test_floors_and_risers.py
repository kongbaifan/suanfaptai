from fastapi.testclient import TestClient

from app.calculators.hydraulic_network import calculate_hydraulic_network
from app.calculators.load import calculate_load
from app.main import app
from app.schemas import LoadRequest, SystemEvaluateRequest

client = TestClient(app)


def _multi_floor_payload():
    return client.get("/api/v1/project/sample/multi_floor").json()


def test_old_payload_without_floors_uses_default_floor():
    response = client.post("/api/v1/system/evaluate", json={"rooms": [{"name": "r1", "area_m2": 50, "people": 5}], "devices": [], "pipes": [], "connections": []})

    assert response.status_code == 200
    body = response.json()
    assert body["load"]["floor_loads"][0]["floor_id"] == "default_floor"
    assert body["topology"]["floor_summary"]["default_floor"]["room_count"] == 1


def test_load_result_groups_rooms_by_floor():
    request = LoadRequest(floors=[{"id": "F1", "level": 1}, {"id": "F2", "level": 2}], rooms=[{"id": "r1", "floor_id": "F1", "area_m2": 100}, {"id": "r2", "floor_id": "F2", "area_m2": 200}])

    result = calculate_load(request)

    loads = {floor.floor_id: floor for floor in result.floor_loads}
    assert loads["F1"].room_count == 1
    assert loads["F2"].area_m2 == 200
    assert loads["F2"].cooling_kw > loads["F1"].cooling_kw


def test_multi_floor_topology_returns_risers_and_floor_summary():
    response = client.post("/api/v1/system/evaluate", json=_multi_floor_payload())

    assert response.status_code == 200
    body = response.json()
    assert body["topology"]["vertical_network"]["risers"]
    assert "F1" in body["topology"]["floor_summary"]
    assert body["load"]["floor_loads"]
    assert body["hydraulic_network"]["floor_flow_summary"]["F1"]["chilled_water"] > 0


def test_non_riser_cross_floor_pipe_is_reported():
    payload = _multi_floor_payload()
    payload["pipes"][2]["from_floor_id"] = "F1"
    payload["pipes"][2]["to_floor_id"] = "F2"
    payload["pipes"][2]["pipe_type"] = "branch_supply"

    response = client.post("/api/v1/project/validate", json=payload)

    assert response.status_code == 200
    codes = {issue["code"] for issue in response.json()["issues"]}
    assert "INVALID_CROSS_FLOOR_PIPE" in codes


def test_typed_hydraulic_flow_uses_main_riser_and_branch_roles():
    request = SystemEvaluateRequest(**_multi_floor_payload())
    result = calculate_hydraulic_network(request, cooling_load_kw=120, heating_load_kw=0)
    flows = {pipe.pipe_id: pipe.flow_m3_h for pipe in result.pipes}

    assert flows["chw_main_supply_b1"] > flows["chw_branch_f1"]
    assert flows["chw_riser_supply"] >= flows["chw_branch_f2"]
    assert result.paths[0].floor_ids


def test_project_save_load_preserves_floor_data():
    payload = _multi_floor_payload()
    create_response = client.post("/api/v1/projects", json={"name": "pytest_floor_project", "project": payload})
    assert create_response.status_code == 200
    project_id = create_response.json()["project_id"]

    try:
        get_response = client.get(f"/api/v1/projects/{project_id}")
        assert get_response.status_code == 200
        assert get_response.json()["project"]["floors"][0]["id"] == "B1F"
    finally:
        client.delete(f"/api/v1/projects/{project_id}")
