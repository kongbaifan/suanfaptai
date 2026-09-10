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
    assert body["role_quality"]["heat_source"]["count"] == 9
    assert body["role_quality"]["heat_source"]["missing_key_fields"] == []
    assert body["role_quality"]["heat_source"]["field_coverage"]["flow_m3_h"] == 1.0
    assert body["role_quality"]["heat_source"]["field_coverage"]["cooling_capacity_kw"] == 1.0
    assert body["role_quality"]["heat_source"]["field_coverage"]["cooling_power_kw"] == 1.0
    assert body["role_quality"]["heat_source"]["field_coverage"]["eer"] == 1.0
    assert body["role_quality"]["pump"]["count"] == 4
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
    total_terminals = sum(1 for item in catalog if item.get("role") == "terminal")
    assert terminal["count"] == total_terminals - len(configurable)
    assert terminal["missing_key_fields"] == []
    assert terminal["field_coverage"]["airflow_m3_h"] == 1.0
    assert terminal["field_coverage"]["fan_power_kw"] == 1.0
    assert [
        issue for issue in body["issues"] if issue["code"] == "CATALOG_KEY_FIELD_MISSING"
    ] == []


def test_catalog_response_uses_chinese_field_names():
    response = client.get("/api/v1/catalog/equipment")

    assert response.status_code == 200
    chiller = next(
        entry
        for entry in response.json()
        if entry["设备类型"] == "冷源机组" and entry["型号"] == "30XW-P0312P"
    )
    assert chiller["设备角色"] == "cold_source"
    assert chiller["额定容量"] == 303.5
    assert chiller["性能系数"] == 5.65
    assert chiller["输入功率"] == 53.8
    assert chiller["冷冻水流量"] == 47.16
    assert chiller["冷却水流量"] == 58.68
    assert chiller["品牌"] == "Carrier"
    assert chiller["兼容旧型号"] == ["LG340MCF"]
    assert chiller["制冷剂"] == "R134a"
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
        if entry["设备类型"] == "水源热泵" and entry["型号"] == "CGS-42(HC)"
    )
    ground_source = next(
        entry
        for entry in catalog
        if entry["设备类型"] == "地源热泵机组" and entry["型号"] == "W-300-HAC"
    )
    air_source = next(
        entry
        for entry in catalog
        if entry["设备类型"] == "空气源热泵" and entry["型号"] == "CGK-080V4-B"
    )

    assert "制冷蒸发器进水温度" not in water_source
    assert "制冷蒸发器出水温度" not in water_source
    assert "制冷冷凝器进水温度" not in water_source
    assert "制冷冷凝器出水温度" not in water_source
    assert water_source["制热蒸发器进水温度"] == 15.0
    assert water_source["制热蒸发器出水温度"] == 10.0
    assert water_source["制热冷凝器进水温度"] == 15.0
    assert water_source["制热冷凝器出水温度"] == 55.0
    assert water_source["额定制冷量"] == 39.1
    assert water_source["制冷输入功率"] == 8.58
    assert water_source["EER"] == 4.56
    assert ground_source["制冷蒸发器进水温度"] == 12.0
    assert ground_source["制冷冷凝器进水温度"] == 25.0
    assert ground_source["制热蒸发器进水温度"] == 0.0
    assert ground_source["制热冷凝器进水温度"] == 40.0
    assert "制冷蒸发器出水温度" not in ground_source
    assert "制热蒸发器出水温度" not in ground_source
    assert ground_source["额定制冷量"] == 77.0
    assert ground_source["制冷输入功率"] == 16.18
    assert ground_source["EER"] == 4.75
    assert ground_source["额定流量"] == 16.2
    assert ground_source["源侧流量"] == 16.2
    assert air_source["制冷蒸发器进水温度"] == 12.0
    assert air_source["制热冷凝器进水温度"] == 30.0
    assert air_source["制热冷凝器出水温度"] == 35.0
    assert air_source["额定制冷量"] == 17.0
    assert air_source["制冷输入功率"] == 6.641
    assert air_source["EER"] == 2.56
    assert air_source["制冷室外干球温度"] == 35.0
    assert air_source["制热室外干球温度"] == 7.0
    assert "制冷冷凝器进水温度" not in air_source
    assert "制热蒸发器进水温度" not in air_source
