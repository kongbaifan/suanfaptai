import base64
from io import BytesIO

from fastapi.testclient import TestClient
from openpyxl import Workbook

from app.data.equipment import load_equipment_catalog
from app.main import app


client = TestClient(app)


def test_catalog_template_endpoint_returns_import_columns():
    response = client.get("/api/v1/catalog/equipment/template")

    assert response.status_code == 200
    body = response.json()
    assert "type" in body["columns"]
    assert "systems" in body["columns"]
    assert "cop" in body["columns"]
    assert "water_pressure_drop_kpa" in body["columns"]
    assert {
        "cooling_capacity_kw",
        "cooling_power_kw",
        "eer",
        "cooling_outdoor_dry_bulb_temp_c",
        "heating_outdoor_dry_bulb_temp_c",
        "cooling_evaporator_inlet_temp_c",
        "cooling_evaporator_outlet_temp_c",
        "cooling_condenser_inlet_temp_c",
        "cooling_condenser_outlet_temp_c",
        "heating_evaporator_inlet_temp_c",
        "heating_evaporator_outlet_temp_c",
        "heating_condenser_inlet_temp_c",
        "heating_condenser_outlet_temp_c",
    } <= set(body["columns"])
    assert body["example"]["type"] == "冷源机组"
    assert body["example"]["name"] == "30XW-P0312P"
    assert "iplv" not in body["columns"]


def test_catalog_upsert_and_delete_item():
    item = {
        "type": "测试设备",
        "name": "T-API-1",
        "role": "terminal",
        "capacity_kw": 12,
        "systems": ["chilled_water"],
    }

    try:
        response = client.post("/api/v1/catalog/equipment/item", json=item)
        assert response.status_code == 200

        catalog = client.get("/api/v1/catalog/equipment").json()
        assert any(entry["设备类型"] == "测试设备" and entry["型号"] == "T-API-1" for entry in catalog)
    finally:
        delete_response = client.delete("/api/v1/catalog/equipment/item/测试设备/T-API-1")
        assert delete_response.status_code == 200


def test_catalog_csv_import_merges_item():
    csv_text = "type,name,role,capacity_kw,water_pressure_drop_kpa,fan_power_kw,systems\n测试导入,CSV-1,terminal,9,18,0.09,chilled_water;air\n"
    payload = {
        "filename": "catalog.csv",
        "content_base64": base64.b64encode(csv_text.encode("utf-8")).decode("ascii"),
        "replace": False,
    }

    try:
        response = client.post("/api/v1/catalog/equipment/import", json=payload)
        assert response.status_code == 200
        assert response.json()["imported"] == 1

        catalog = client.get("/api/v1/catalog/equipment").json()
        imported = next(entry for entry in catalog if entry["设备类型"] == "测试导入" and entry["型号"] == "CSV-1")
        assert imported["额定容量"] == 9
        assert imported["水侧压降"] == 18
        assert imported["风机功率"] == 0.09
        assert imported["适用系统"] == ["chilled_water", "air"]
    finally:
        client.delete("/api/v1/catalog/equipment/item/测试导入/CSV-1")


def test_catalog_xlsx_import_merges_item():
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["type", "name", "role", "airflow_m3_h", "systems"])
    sheet.append(["测试导入", "XLSX-1", "terminal", 1200, "air"])
    buffer = BytesIO()
    workbook.save(buffer)

    payload = {
        "filename": "catalog.xlsx",
        "content_base64": base64.b64encode(buffer.getvalue()).decode("ascii"),
        "replace": False,
    }

    try:
        response = client.post("/api/v1/catalog/equipment/import", json=payload)
        assert response.status_code == 200
        assert response.json()["imported"] == 1

        catalog = client.get("/api/v1/catalog/equipment").json()
        imported = next(entry for entry in catalog if entry["设备类型"] == "测试导入" and entry["型号"] == "XLSX-1")
        assert imported["额定风量"] == 1200
        assert imported["适用系统"] == ["air"]
    finally:
        client.delete("/api/v1/catalog/equipment/item/测试导入/XLSX-1")


def test_catalog_bad_import_returns_structured_issue():
    payload = {
        "filename": "catalog.json",
        "content_base64": base64.b64encode(b"{bad json").decode("ascii"),
        "replace": False,
    }

    response = client.post("/api/v1/catalog/equipment/import", json=payload)

    assert response.status_code == 200
    assert response.json()["imported"] == 0
    assert response.json()["issues"][0]["code"] == "CATALOG_IMPORT_PARSE_ERROR"


def test_catalog_quality_endpoint_reports_coverage():
    response = client.get("/api/v1/catalog/equipment/quality")

    assert response.status_code == 200
    body = response.json()
    assert body["total"] > 0
    assert "cold_source" in body["role_quality"]
    assert "cop" in body["role_quality"]["cold_source"]["field_coverage"]
    # 热泵家族 39 台：制热工况来自厂家样本，制冷工况由能量平衡派生
    # （制冷量 = 制热量 − 输入功率，EER 按同功率推算），均标 derived_fields。
    assert body["role_quality"]["heat_source"]["count"] == 39
    assert body["role_quality"]["heat_source"]["missing_key_fields"] == []
    for field in ("capacity_kw", "flow_m3_h", "power_kw", "cop",
                  "cooling_capacity_kw", "cooling_power_kw", "eer"):
        assert body["role_quality"]["heat_source"]["field_coverage"][field] == 1.0
    assert body["role_quality"]["pump"]["count"] == 7
    assert body["role_quality"]["pump"]["missing_key_fields"] == []


def test_catalog_quality_excludes_configurable_series_from_coverage():
    """按项目选型的系列条目没有具体规格参数，不应计入覆盖率或产生缺字段问题。"""
    catalog = load_equipment_catalog()
    configurable = [item for item in catalog if item.get("configurable_series")]
    assert configurable, "目录应至少包含一个按项目选型的系列条目"
    assert {str(item.get("role")) for item in configurable} == {"terminal"}

    response = client.get("/api/v1/catalog/equipment/quality")

    assert response.status_code == 200
    body = response.json()
    terminal = body["role_quality"]["terminal"]
    # 系列条目与摆放条目都不计入覆盖率：只统计有具体规格的工程末端（当前 54 条）。
    engineering_terminals = [
        item
        for item in catalog
        if item.get("role") == "terminal" and not item.get("placement_only")
    ]
    assert terminal["count"] == len(engineering_terminals) - len(configurable)
    assert terminal["field_coverage"]["airflow_m3_h"] == 1.0
    # 工程末端必备字段已全部补齐（组合式机组水侧/风机功率按风量与水侧
    # 定额派生），缺字段告警整体清零，系列条目自然不产生告警。
    assert [
        issue for issue in body["issues"] if issue["code"] == "CATALOG_KEY_FIELD_MISSING"
    ] == []


def test_catalog_response_uses_chinese_field_names():
    response = client.get("/api/v1/catalog/equipment")

    assert response.status_code == 200
    chiller = next(
        entry
        for entry in response.json()
        if entry["设备类型"] == "冷源机组" and entry["型号"] == "SCWE-100H+B"
    )
    assert chiller["设备角色"] == "cold_source"
    assert chiller["额定容量"] == 333.8
    assert chiller["性能系数"] == 5.81
    assert chiller["输入功率"] == 57.41
    assert chiller["冷冻水流量"] == 57.42
    assert chiller["冷却水流量"] == 71.77
    assert chiller["系列"] == "SCWE 高效满液"
    assert chiller["适用系统"] == ["chilled_water", "cooling_water"]
    assert "capacity_kw" not in chiller
    assert "type" not in chiller


def test_catalog_response_exposes_heat_pump_operating_data():
    response = client.get("/api/v1/catalog/equipment")

    assert response.status_code == 200
    catalog = response.json()
    water_source = next(
        entry
        for entry in catalog
        if entry["设备类型"] == "水源热泵" and entry["型号"] == "SHWE-100H-地下水"
    )
    ground_source = next(
        entry
        for entry in catalog
        if entry["设备类型"] == "地源热泵" and entry["型号"] == "SHWE-100H-地埋管"
    )
    air_source = next(
        entry
        for entry in catalog
        if entry["设备类型"] == "空气源热泵" and entry["型号"] == "SHAF-80H"
    )

    # 运行工况温度字段目前无样本数据，目录不伪造空值
    assert "制冷蒸发器进水温度" not in water_source
    assert "制冷蒸发器出水温度" not in water_source
    assert "制热蒸发器进水温度" not in water_source
    assert water_source["额定容量"] == 362.5
    assert water_source["性能系数"] == 6.78
    assert water_source["额定制热量"] == 375.7
    assert water_source["制热输入功率"] == 76.16
    assert water_source["额定流量"] == 62.36
    assert ground_source["额定制热量"] == 358.9
    assert ground_source["制热输入功率"] == 75.52
    assert air_source["额定容量"] == 264.2
    assert air_source["额定制热量"] == 270.7
    assert air_source["性能系数"] == 3.43
