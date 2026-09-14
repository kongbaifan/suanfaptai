from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health():
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_water_endpoint():
    response = client.post("/api/v1/water/calculate", json={"load_kw": 100, "delta_t_c": 5})

    assert response.status_code == 200
    body = response.json()
    assert body["flow_m3_h"] > 0
    assert body["selected_pipe"]["dn"] > 0


def test_invalid_negative_load_is_rejected():
    response = client.post("/api/v1/water/calculate", json={"load_kw": -1})

    assert response.status_code == 422


def test_system_evaluate_flags_more_issues_for_incomplete_system():
    bad = {
        "rooms": [{"name": "r1", "area_m2": 100, "people": 10}],
        "devices": [],
        "pipes": [],
        "connections": [],
    }
    good = {
        "rooms": [{"name": "r1", "area_m2": 100, "people": 10}],
        "devices": [
            {"id": "chiller_1", "type": "冷源机组", "capacity_kw": 340},
            {"id": "pump_1", "type": "循环泵"},
            {"id": "tower_1", "type": "冷却塔", "capacity_kw": 100},
            {"id": "fcu_1", "type": "风机盘管", "capacity_kw": 5, "quantity": 10},
        ],
        "pipes": [{"id": "pipe_1", "system": "chilled_water", "length_m": 20}],
        "connections": [
            {"from_id": "chiller_1", "to_id": "pump_1", "system": "chilled_water"},
            {"from_id": "pump_1", "to_id": "pipe_1", "system": "chilled_water"},
            {"from_id": "pipe_1", "to_id": "fcu_1", "system": "chilled_water"},
            {"from_id": "fcu_1", "to_id": "chiller_1", "system": "chilled_water"},
        ],
    }

    bad_response = client.post("/api/v1/system/evaluate", json=bad)
    good_response = client.post("/api/v1/system/evaluate", json=good)

    assert bad_response.status_code == 200
    assert good_response.status_code == 200
    # 空系统缺核心设备，完整系统不该报这三条。评分去掉后按 code 判定，
    # 不比 issue 总数：两者报出的 issue 集合本就不同（完整系统会报
    # 水力/风量/参数降级等质量问题，空系统只报缺件）。
    missing_codes = {"NO_DEVICES", "MISSING_COLD_SOURCE", "MISSING_TERMINAL"}
    bad_codes = {issue["code"] for issue in bad_response.json()["issues"]}
    good_codes = {issue["code"] for issue in good_response.json()["issues"]}
    assert missing_codes <= bad_codes
    assert not missing_codes & good_codes
    assert "report" in good_response.json()


def test_device_ports_chiller_by_model():
    response = client.get(
        "/api/v1/devices/ports", params={"type": "冷源机组", "model": "SCWE-90H+B"}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["role"] == "chiller"
    assert body["source"] == "catalog"
    assert body["model_name"] == "SCWE-90H+B"
    assert body["dynamic_ports"] is False
    assert [(port["direction"], port["medium"]) for port in body["ports"]] == [
        ("return", "chilled_water"),
        ("supply", "chilled_water"),
        ("supply", "cooling_water"),
        ("return", "cooling_water"),
    ]
    assert all(port["optional"] is False for port in body["ports"])


def test_device_ports_model_alias_hits_canonical_name():
    # MCW200V 是 FP-34WA 的别名，命中后回显规范型号名
    response = client.get(
        "/api/v1/devices/ports", params={"type": "风机盘管", "model": "MCW200V"}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "catalog"
    assert body["model_name"] == "FP-34WA"
    assert body["role"] == "fcu"


def test_device_ports_model_only_in_unity_library_resolves_by_type():
    # LG340MCF 只在 Unity 数字键库里，文字参数库查不到 → 按类型解析
    response = client.get(
        "/api/v1/devices/ports", params={"type": "冷源机组", "model": "LG340MCF"}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "type_default"
    assert len(body["ports"]) == 4


def test_device_ports_fcu_marks_hot_water_optional():
    response = client.get("/api/v1/devices/ports", params={"type": "风机盘管"})

    assert response.status_code == 200
    body = response.json()
    assert body["role"] == "fcu"
    assert body["source"] == "type_default"
    ports = body["ports"]
    assert len(ports) == 6
    optional = [port for port in ports if port["optional"]]
    assert [(port["port_index"], port["medium"]) for port in optional] == [
        (2, "hot_water"),
        (3, "hot_water"),
    ]


def test_device_ports_pump_medium_pending():
    response = client.get("/api/v1/devices/ports", params={"type": "循环泵"})

    assert response.status_code == 200
    body = response.json()
    assert body["role"] == "pump"
    assert [port["medium"] for port in body["ports"]] == [None, None]
    assert [port["direction"] for port in body["ports"]] == ["return", "supply"]


def test_device_ports_manifold_is_dynamic():
    response = client.get("/api/v1/devices/ports", params={"type": "分水器"})

    assert response.status_code == 200
    body = response.json()
    assert body["role"] == "manifold"
    assert body["dynamic_ports"] is True
    assert len(body["ports"]) == 1


def test_device_ports_unknown_type_is_rejected():
    response = client.get("/api/v1/devices/ports", params={"type": "电梯"})

    assert response.status_code == 404


def test_device_ports_unknown_model_falls_back_to_type():
    response = client.get(
        "/api/v1/devices/ports", params={"type": "冷源机组", "model": "查无此型"}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "type_default"
    assert len(body["ports"]) == 4


def _blank_project_payload(name: str = "api-project-test") -> dict:
    return {
        "name": name,
        "project": {"rooms": [], "devices": [], "pipes": [], "connections": []},
    }


def test_project_delete_unknown_returns_404_not_500():
    """删除不存在的项目应返回 404；曾因清理函数先校验项目而报 500。"""
    response = client.delete("/api/v1/projects/not_a_real_project")

    assert response.status_code == 404


def test_project_duplicate_copies_content_and_names_uniquely():
    created = client.post("/api/v1/projects", json=_blank_project_payload("复制源")).json()
    project_id = created["project_id"]
    try:
        first = client.post(f"/api/v1/projects/{project_id}/duplicate")
        assert first.status_code == 200
        assert first.json()["name"] == "复制源 副本"
        assert first.json()["project"] == created["project"]

        second = client.post(f"/api/v1/projects/{project_id}/duplicate")
        assert second.json()["name"] == "复制源 副本 2"

        assert client.post(
            "/api/v1/projects/not_a_real_project/duplicate"
        ).status_code == 404
    finally:
        client.delete(f"/api/v1/projects/{project_id}")


def test_project_delete_removes_it_from_listing():
    created = client.post("/api/v1/projects", json=_blank_project_payload("待删除")).json()
    project_id = created["project_id"]

    assert client.delete(f"/api/v1/projects/{project_id}").json() == {"deleted": True}
    listing = client.get("/api/v1/projects").json()
    assert all(item["project_id"] != project_id for item in listing)
