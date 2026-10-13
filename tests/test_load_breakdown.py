"""逐房间冷负荷表接口。

钉住四件事：

1. **数值来自现有分项法**：`three_floor_retail` 样例的表格逐格与教学界面一致
   （房间行、楼层小计、全楼合计、修正后 × 0.990），且每一行都满足
   "七个冷分项之和 = 合计"，表里的数字可以直接相加核对。
2. **聚合行的人员列是冷负荷**：截图里小计/合计行的"人员"格放的是人数（75.00 /
   215.00），这里是 Σ 各房间人员冷负荷（9.00 / 25.80）——同一个数乘 0.99 得到
   修正后的 25.54，而不是 212.85。
3. **面积和人数可以改**：请求体版直接改 `rooms`，项目版用 overrides 改，
   都按商场手算值（人员 0.12 kW/人、照明 18 W/m²、设备 20 W/m²、人均新风 25 m³/h）核对。
4. **改不动要报错**：定位不到房间、空改动一律 422，不静默忽略。
"""

from __future__ import annotations

from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from app.calculators.load import build_room_load_breakdown
from app.main import app
from app.samples import THREE_FLOOR_RETAIL_SAMPLE
from app.schemas import LoadRequest, RoomInput, SystemEvaluateRequest

client = TestClient(app)
KW = pytest.approx

_EXPECTED_COLUMNS = [
    "people",
    "lighting",
    "equipment",
    "solar",
    "envelope_cooling",
    "fresh_air_cooling",
    "latent_cooling",
]
_EXPECTED_NAMES = [
    "一层北区（扶梯）",
    "一层南区",
    "一层小计",
    "二层中庭",
    "二层南区",
    "二层东区",
    "二层西北区",
    "二层西南区",
    "二层小计",
    "三层中庭",
    "三层西区",
    "三层南区",
    "三层东区",
    "三层小计",
    "全楼合计",
    "修正后（× 0.990）",
]
_EXPECTED_TYPES = (
    ["room"] * 2
    + ["floor_subtotal"]
    + ["room"] * 5
    + ["floor_subtotal"]
    + ["room"] * 4
    + ["floor_subtotal"]
    + ["building_total", "adjusted_total"]
)


def _retail_request() -> LoadRequest:
    project = SystemEvaluateRequest(**deepcopy(THREE_FLOOR_RETAIL_SAMPLE))
    return LoadRequest(
        rooms=project.rooms,
        floors=project.floors,
        simultaneity_factor=project.simultaneity_factor,
        safety_factor=project.safety_factor,
    )


def _row(result, key: str):
    return next(row for row in result.rows if row.key == key)


def test_breakdown_row_order_and_columns():
    """行序 = 一层房间→小计→二层…→全楼合计→修正后；列序固定为七分项。"""
    result = build_room_load_breakdown(_retail_request())

    assert result.component_keys == _EXPECTED_COLUMNS
    assert [row.name for row in result.rows] == _EXPECTED_NAMES
    assert [row.row_type for row in result.rows] == _EXPECTED_TYPES
    assert result.factors.simultaneity_factor == KW(0.9)
    assert result.factors.safety_factor == KW(1.1)
    assert result.factors.combined_factor == KW(0.99)
    assert result.total_area_m2 == KW(1722.0)
    assert result.total_people == 215


def test_breakdown_matches_teaching_table():
    """逐格钉死教学界面的数字（样例默认系数 0.9 × 1.1 = 0.990）。"""
    result = build_room_load_breakdown(_retail_request())

    first = result.rows[0]
    assert first.key == "f1_north"
    assert first.floor_id == "F1"
    assert first.floor_name == "一层"
    assert first.area_m2 == KW(264.0)
    assert first.people == 33
    assert first.components_kw == KW(
        {
            "people": 3.96,
            "lighting": 4.75,
            "equipment": 5.28,
            "solar": 0.40,
            "envelope_cooling": 3.99,
            "fresh_air_cooling": 2.49,
            "latent_cooling": 8.06,
        },
        abs=1e-2,
    )
    assert first.cooling_kw == KW(28.93, abs=1e-2)
    assert first.cooling_index_w_m2 == KW(109.6, abs=0.05)
    assert first.adjusted_cooling_kw == KW(28.64, abs=1e-2)
    assert first.fresh_air_m3_h == KW(33 * 25)

    floor = _row(result, "F1")
    assert floor.row_type == "floor_subtotal"
    assert floor.name == "一层小计"
    assert floor.area_m2 == KW(602.0)
    assert floor.people == 75
    # 人员列是冷负荷，不是人数：3.96 + 5.04 = 9.00（截图这里显示的是 75.00）。
    assert floor.components_kw["people"] == KW(9.0, abs=1e-2)
    assert floor.cooling_kw == KW(66.40, abs=1e-2)
    assert floor.cooling_index_w_m2 == KW(110.3, abs=0.05)

    total = _row(result, "ALL")
    assert total.row_type == "building_total"
    assert total.area_m2 == KW(1722.0)
    assert total.people == 215
    assert total.components_kw == KW(
        {
            "people": 25.80,
            "lighting": 31.00,
            "equipment": 34.44,
            "solar": 6.30,
            "envelope_cooling": 26.04,
            "fresh_air_cooling": 16.20,
            "latent_cooling": 52.49,
        },
        abs=1e-2,
    )
    assert total.cooling_kw == KW(192.26, abs=1e-2)
    # 指标按行里显示的合计/面积算：192.265 / 1722 × 1000 = 111.65 → 111.7（界面取整显示 112）。
    assert total.cooling_index_w_m2 == KW(111.7, abs=0.05)

    adjusted = _row(result, "ALL_ADJUSTED")
    assert adjusted.row_type == "adjusted_total"
    assert adjusted.area_m2 is None
    assert adjusted.people is None
    assert adjusted.components_kw == KW(
        {
            "people": 25.54,
            "lighting": 30.69,
            "equipment": 34.10,
            "solar": 6.24,
            "envelope_cooling": 25.78,
            "fresh_air_cooling": 16.04,
            "latent_cooling": 51.96,
        },
        abs=1e-2,
    )
    assert adjusted.cooling_kw == KW(190.34, abs=1e-2)
    assert adjusted.cooling_kw == KW(total.cooling_kw * 0.99, abs=1e-2)


def test_every_row_adds_up_and_subtotals_match_total():
    """分项之和 = 合计；三个楼层小计之和 = 全楼合计（教学表要能直接相加核对）。"""
    result = build_room_load_breakdown(_retail_request())

    for row in result.rows:
        assert sum(row.components_kw.values()) == KW(row.cooling_kw, abs=2e-2), row.name

    subtotals = [row for row in result.rows if row.row_type == "floor_subtotal"]
    total = _row(result, "ALL")
    assert len(subtotals) == 3
    assert sum(row.area_m2 for row in subtotals) == KW(total.area_m2, abs=1e-3)
    assert sum(row.people for row in subtotals) == total.people
    for key in _EXPECTED_COLUMNS:
        assert sum(row.components_kw[key] for row in subtotals) == KW(
            total.components_kw[key], abs=2e-2
        )
    assert sum(row.cooling_kw for row in subtotals) == KW(total.cooling_kw, abs=2e-2)


def test_edited_area_and_people_change_the_table():
    """面积/人数是接口输入：改完立即反映到分项（商场：0.12 kW/人、18/20 W/m²）。"""
    result = build_room_load_breakdown(
        LoadRequest(rooms=[RoomInput(id="r1", name="r1", area_m2=300, people=40, purpose="mall")])
    )

    room = result.rows[0]
    assert room.area_m2 == KW(300.0)
    assert room.people == 40
    assert room.components_kw["people"] == KW(40 * 0.12, abs=1e-3)
    assert room.components_kw["lighting"] == KW(300 * 18 / 1000, abs=1e-3)
    assert room.components_kw["equipment"] == KW(300 * 20 / 1000, abs=1e-3)
    assert room.fresh_air_m3_h == KW(40 * 25)
    # 围护 = 1.2 × (300 × 1.4) × (35 − 26) / 1000
    assert room.components_kw["envelope_cooling"] == KW(4.536, abs=1e-3)

    # 面积/人数不动时，同一房间重复调用必须完全一致。
    again = build_room_load_breakdown(
        LoadRequest(rooms=[RoomInput(id="r1", name="r1", area_m2=300, people=40, purpose="mall")])
    )
    assert again.rows == result.rows


def test_breakdown_endpoint_accepts_editable_rooms():
    response = client.post(
        "/api/v1/load/room-breakdown",
        json={
            "rooms": [
                {"id": "r1", "name": "营业厅", "area_m2": 300, "people": 40, "purpose": "mall"},
                {"id": "r2", "name": "库房", "area_m2": 100, "people": 0, "purpose": "mall"},
            ]
        },
    )

    assert response.status_code == 200
    body = response.json()
    # 无楼层声明时归入默认楼层：2 个房间 + 1 个小计 + 全楼合计 + 修正后。
    assert [row["row_type"] for row in body["rows"]] == [
        "room",
        "room",
        "floor_subtotal",
        "building_total",
        "adjusted_total",
    ]
    assert body["rows"][0]["area_m2"] == 300.0
    assert body["rows"][0]["people"] == 40
    assert body["total_area_m2"] == 400.0
    assert body["rows"][-1]["name"] == "修正后（× 0.990）"


def _create_retail_project() -> str:
    created = client.post(
        "/api/v1/projects",
        json={"name": "逐房间负荷表测试", "project": deepcopy(THREE_FLOOR_RETAIL_SAMPLE)},
    )
    assert created.status_code == 200
    return created.json()["project_id"]


def test_project_breakdown_reads_saved_project_and_applies_overrides():
    project_id = _create_retail_project()
    try:
        plain = client.post(f"/api/v1/projects/{project_id}/load/room-breakdown")
        assert plain.status_code == 200
        assert [row["name"] for row in plain.json()["rows"]] == _EXPECTED_NAMES
        assert plain.json()["rows"][0]["area_m2"] == 264.0

        edited = client.post(
            f"/api/v1/projects/{project_id}/load/room-breakdown",
            json={
                "rooms": [
                    {"room_id": "f1_north", "area_m2": 300, "people": 40},
                    {"index": 1, "area_m2": 300},
                ],
                "simultaneity_factor": 1.0,
                "safety_factor": 1.0,
            },
        )
        assert edited.status_code == 200
        body = edited.json()
        first = body["rows"][0]
        assert first["area_m2"] == 300.0
        assert first["people"] == 40
        assert first["components_kw"]["people"] == KW(4.8, abs=1e-3)
        assert first["components_kw"]["lighting"] == KW(5.4, abs=1e-3)
        assert body["rows"][1]["area_m2"] == 300.0
        assert body["rows"][1]["people"] == 42
        # 一层两个房间都改成 300 m²：300 + 300 + 二层 561 + 三层 559。
        assert body["total_area_m2"] == KW(1720.0, abs=1e-3)
        # 系数改成 1.0：修正后行与全楼合计完全一致。
        assert body["factors"]["combined_factor"] == KW(1.0)
        assert body["rows"][-1]["cooling_kw"] == KW(body["rows"][-2]["cooling_kw"], abs=1e-3)

        # 改动不落库：项目本身还是原值。
        stored = client.get(f"/api/v1/projects/{project_id}").json()
        assert stored["project"]["rooms"][0]["area_m2"] == 264.0
        assert stored["project"]["rooms"][0]["people"] == 33
    finally:
        client.delete(f"/api/v1/projects/{project_id}")


def test_project_breakdown_rejects_unusable_overrides():
    project_id = _create_retail_project()
    try:
        unknown_room = client.post(
            f"/api/v1/projects/{project_id}/load/room-breakdown",
            json={"rooms": [{"room_id": "no_such_room", "area_m2": 300}]},
        )
        assert unknown_room.status_code == 422

        no_target = client.post(
            f"/api/v1/projects/{project_id}/load/room-breakdown",
            json={"rooms": [{"area_m2": 300}]},
        )
        assert no_target.status_code == 422

        empty_change = client.post(
            f"/api/v1/projects/{project_id}/load/room-breakdown",
            json={"rooms": [{"room_id": "f1_north"}]},
        )
        assert empty_change.status_code == 422

        mismatched_index = client.post(
            f"/api/v1/projects/{project_id}/load/room-breakdown",
            json={"rooms": [{"index": 0, "room_id": "f1_south", "area_m2": 300}]},
        )
        assert mismatched_index.status_code == 422

        out_of_range = client.post(
            f"/api/v1/projects/{project_id}/load/room-breakdown",
            json={"rooms": [{"index": 99, "area_m2": 300}]},
        )
        assert out_of_range.status_code == 422

        # 面积必须为正、人数不能为负——由 schema 拦下。
        assert client.post(
            f"/api/v1/projects/{project_id}/load/room-breakdown",
            json={"rooms": [{"room_id": "f1_north", "area_m2": 0}]},
        ).status_code == 422
        assert client.post(
            f"/api/v1/projects/{project_id}/load/room-breakdown",
            json={"rooms": [{"room_id": "f1_north", "people": -1}]},
        ).status_code == 422

        assert client.post(
            "/api/v1/projects/not_a_real_project/load/room-breakdown"
        ).status_code == 404
    finally:
        client.delete(f"/api/v1/projects/{project_id}")
