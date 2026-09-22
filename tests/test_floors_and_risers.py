from fastapi.testclient import TestClient

from app.calculators.air_network import _path_floor_ids as air_path_floor_ids
from app.calculators.hydraulic_network import (
    _path_floor_ids as hydraulic_path_floor_ids,
)
from app.calculators.hydraulic_network import calculate_hydraulic_network
from app.calculators.load import calculate_load
from app.calculators.topology import _path_floor_ids
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


def test_path_floor_ids_are_a_dedup_of_the_nodes_own_floors():
    """路径的 floor_ids 是所经节点归属楼层的**去重**，不做任何楼层过滤

    路径的 nodes 里只要有这个设备，它的归属楼层就必须出现在 floor_ids 里，
    否则两者对不上，调用方按 floor_ids 反查楼层会查不到东西。

    没写 `floor_id` 的设备归属**项目里真实存在的第一个楼层**（见 building.py
    的 `implicit_floor_id`），而不是字面量 `default_floor`——后者是项目里根本
    不存在的楼层，会被 `validators/floors.py` 判成 error。本用例的两台设备
    因此同属 `floor-1`，去重后只剩一个。

    历史：这里原先期望 `["floor-1", "default_floor"]`，钉的正是那条错误的
    兜底（`DEFAULT_FLOOR_ID` 从来不是"有效楼层"，而是"兜底值"）。

    topology / air_network / hydraulic_network 三个模块各有一份同名实现，此处
    钉死它们必须一致（原先 topology 那份多出一个恒真的条件，虽不影响结果，
    但使三份实现看起来语义不同）。
    """
    request = SystemEvaluateRequest(
        floors=[{"id": "floor-1", "name": "F1", "level": 1}],
        rooms=[{"id": "r1", "name": "r1", "area_m2": 10, "floor_id": "floor-1"}],
        devices=[
            {"id": "fcu_explicit", "type": "风机盘管", "name": "*", "floor_id": "floor-1"},
            {"id": "fcu_default", "type": "风机盘管", "name": "*"},
        ],
        pipes=[],
        connections=[],
    )
    nodes = ["fcu_explicit", "fcu_default"]

    expected = ["floor-1"]
    assert _path_floor_ids(request, nodes) == expected
    assert air_path_floor_ids(request, nodes) == expected
    assert hydraulic_path_floor_ids(request, nodes) == expected


def test_path_floor_ids_dedupes_repeated_floors():
    """同一楼层的多个节点只记一次，且保持首次出现顺序"""
    request = SystemEvaluateRequest(
        floors=[{"id": "F1", "name": "F1", "level": 1}, {"id": "F2", "name": "F2", "level": 2}],
        rooms=[],
        devices=[
            {"id": "a", "type": "风机盘管", "name": "*", "floor_id": "F1"},
            {"id": "b", "type": "风机盘管", "name": "*", "floor_id": "F2"},
            {"id": "c", "type": "风机盘管", "name": "*", "floor_id": "F1"},
        ],
        pipes=[],
        connections=[],
    )

    assert _path_floor_ids(request, ["a", "b", "c"]) == ["F1", "F2"]
    assert _path_floor_ids(request, []) == []


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
