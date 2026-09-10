from fastapi.testclient import TestClient

from app.calculators.load import calculate_load
from app.main import app
from app.schemas import LoadRequest, RoomInput


client = TestClient(app)


def test_project_sample_endpoint_returns_complete_sample():
    response = client.get("/api/v1/project/sample/complete")

    assert response.status_code == 200
    assert response.json()["rooms"]
    assert response.json()["devices"]


def test_project_validate_detects_duplicate_ids():
    payload = {
        "rooms": [{"name": "r1", "area_m2": 30}],
        "devices": [{"id": "node_1", "type": "冷源机组"}, {"id": "node_1", "type": "循环泵"}],
        "pipes": [],
        "connections": [],
    }

    response = client.post("/api/v1/project/validate", json=payload)

    assert response.status_code == 200
    body = response.json()
    assert body["valid"] is False
    assert "DUPLICATE_ID" in {issue["code"] for issue in body["issues"]}


def test_catalog_and_rules_endpoints():
    catalog = client.get("/api/v1/catalog/equipment")
    rules = client.get("/api/v1/rules")

    assert catalog.status_code == 200
    assert rules.status_code == 200
    assert any(item["设备类型"] == "冷源机组" for item in catalog.json())
    assert "water_velocity_m_s" in rules.json()


def test_report_download_endpoint_returns_json_attachment():
    sample = client.get("/api/v1/project/sample/complete").json()
    response = client.post("/api/v1/report/download", json=sample)

    assert response.status_code == 200
    assert "attachment" in response.headers["content-disposition"]
    assert response.json()["summary"]["device_count"] > 0


def test_enhanced_load_uses_orientation_and_purpose():
    north = calculate_load(LoadRequest(rooms=[RoomInput(name="north", area_m2=100, people=10, purpose="office", orientation="north")]))
    west = calculate_load(LoadRequest(rooms=[RoomInput(name="west", area_m2=100, people=10, purpose="office", orientation="west")]))
    meeting = calculate_load(LoadRequest(rooms=[RoomInput(name="meeting", area_m2=100, people=10, purpose="meeting")]))

    assert west.total_cooling_kw > north.total_cooling_kw
    assert meeting.total_cooling_kw > north.total_cooling_kw


def test_mall_purpose_presets_apply_design_defaults():
    """商场类细分用途应有各自的设计指标：超市最密、中庭照明最重、
    未登记用途回落房间自身默认值。"""
    office = calculate_load(LoadRequest(rooms=[RoomInput(name="a", area_m2=100, people=10, purpose="office")]))
    supermarket = calculate_load(LoadRequest(rooms=[RoomInput(name="b", area_m2=100, people=10, purpose="supermarket")]))
    restaurant = calculate_load(LoadRequest(rooms=[RoomInput(name="c", area_m2=100, people=10, purpose="restaurant")]))
    atrium = calculate_load(LoadRequest(rooms=[RoomInput(name="d", area_m2=100, people=10, purpose="atrium")]))
    shop = calculate_load(LoadRequest(rooms=[RoomInput(name="e", area_m2=100, people=10, purpose="shop")]))
    unknown = calculate_load(LoadRequest(rooms=[RoomInput(name="f", area_m2=100, people=10, purpose="warehouse")]))

    # 面积指标分项 = 冷指标 × 面积 / 1000
    assert supermarket.rooms[0].components_kw["area_index"] == 20.0
    assert restaurant.rooms[0].components_kw["area_index"] == 19.0
    assert atrium.rooms[0].components_kw["area_index"] == 16.0
    assert shop.rooms[0].components_kw["area_index"] == 17.5
    # 商场类都应高于办公
    for result in (supermarket, restaurant, shop):
        assert result.total_cooling_kw > office.total_cooling_kw
    # 未登记用途回落 RoomInput 默认（冷指标 120）
    assert unknown.rooms[0].components_kw["area_index"] == 12.0
    # 中庭人均新风 15，照明 25 W/m²
    assert atrium.rooms[0].fresh_air_m3_h == 150.0
    assert atrium.rooms[0].components_kw["lighting"] == 2.5


def test_project_save_load_evaluate_history_and_delete():
    sample = client.get("/api/v1/project/sample/complete").json()
    create_response = client.post(
        "/api/v1/projects",
        json={"name": "pytest_project", "project": sample, "description": "temporary", "tags": ["test"]},
    )

    assert create_response.status_code == 200
    project_id = create_response.json()["project_id"]

    try:
        list_response = client.get("/api/v1/projects")
        assert list_response.status_code == 200
        assert any(project["project_id"] == project_id for project in list_response.json())

        get_response = client.get(f"/api/v1/projects/{project_id}")
        assert get_response.status_code == 200
        assert get_response.json()["name"] == "pytest_project"

        eval_response = client.post(f"/api/v1/projects/{project_id}/evaluate")
        assert eval_response.status_code == 200
        assert "score" not in eval_response.json()

        history_response = client.get(f"/api/v1/projects/{project_id}/history")
        assert history_response.status_code == 200
        history = history_response.json()
        assert len(history) == 1
        assert history[0]["issue_count"] == len(eval_response.json()["issues"])
    finally:
        delete_response = client.delete(f"/api/v1/projects/{project_id}")
        assert delete_response.status_code == 200


def test_blank_project_can_be_created_saved_and_read_back():
    """空白项目：全部清单为空时也应能建项目并读回；校验只报告结构缺失，不阻塞创建。"""
    create_response = client.post(
        "/api/v1/projects",
        json={"name": "pytest_blank_project", "project": {}, "tags": ["test"]},
    )

    assert create_response.status_code == 200
    project_id = create_response.json()["project_id"]

    try:
        get_response = client.get(f"/api/v1/projects/{project_id}")
        assert get_response.status_code == 200
        body = get_response.json()
        assert body["project"]["floors"] == []
        assert body["project"]["rooms"] == []
        assert body["project"]["devices"] == []
        assert body["project"]["pipes"] == []
        assert body["project"]["connections"] == []

        # 结构校验对空白项目返回"不完整"提示（错误级 Issue），但创建/读回不受影响。
        validate_response = client.post("/api/v1/project/validate", json={})
        assert validate_response.status_code == 200
        validate_body = validate_response.json()
        assert validate_body["valid"] is False
        issue_codes = {issue["code"] for issue in validate_body["issues"]}
        assert "NO_DEVICES" in issue_codes
        assert "MISSING_COLD_SOURCE" in issue_codes
        assert "MISSING_TERMINAL" in issue_codes
    finally:
        delete_response = client.delete(f"/api/v1/projects/{project_id}")
        assert delete_response.status_code == 200
