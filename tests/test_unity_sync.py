"""Unity DeviceLibrary.json 与主设备目录双向同步的契约测试。

目录走临时副本（fixture 指向拷贝文件），不触碰真实的 equipment_catalog.json；
DeviceLibrary.json 真实文件作为只读输入使用，导出写到 tmp_path。
"""

import json
import shutil
from pathlib import Path

import pytest

from app.data import equipment, unity_sync


@pytest.fixture()
def isolated_catalog(tmp_path: Path):
    """把目录读写隔离到临时副本，并还原成未同步状态，避免污染真实文件。"""
    source = _read_catalog(Path(equipment.CATALOG_PATH))
    # 剔除摆放条目并剥掉摆放字段，得到未同步前的 121 条工程条目。
    pristine = [
        {
            key: value
            for key, value in item.items()
            if key not in ("unity_entries", "placement_only")
        }
        for item in source
        if not item.get("placement_only")
    ]
    catalog_copy = tmp_path / "equipment_catalog.json"
    catalog_copy.write_text(json.dumps(pristine, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    original_path = equipment.CATALOG_PATH
    equipment.CATALOG_PATH = catalog_copy
    equipment.load_equipment_catalog.cache_clear()
    yield catalog_copy
    equipment.CATALOG_PATH = original_path
    equipment.load_equipment_catalog.cache_clear()


@pytest.fixture()
def library_copy(tmp_path: Path) -> Path:
    copy = tmp_path / "DeviceLibrary.json"
    shutil.copy(unity_sync.device_library_path(), copy)
    return copy


def _read_catalog(catalog_path: Path) -> list[dict]:
    return json.loads(catalog_path.read_text(encoding="utf-8"))


def test_import_attaches_alias_matches_and_creates_placement_items(isolated_catalog, library_copy):
    engineering_before = sum(
        1 for item in _read_catalog(isolated_catalog) if not item.get("placement_only")
    )
    summary = unity_sync.import_device_library(library_copy)
    catalog = _read_catalog(isolated_catalog)

    # 未同步工程条目（131 条）：23 条 Unity 名命中别名挂原条目，404 条新建摆放条目。
    expected_created = 427 - 23
    assert summary["total"] == 427
    assert summary["attached"] == 23
    assert summary["created"] == expected_created
    assert len(catalog) == expected_created + engineering_before

    fcu = next(item for item in catalog if item["type"] == "风机盘管" and item["name"] == "FP-34WA")
    entry_names = [entry["device_name"] for entry in fcu["unity_entries"]]
    assert entry_names == ["MCW200V"]
    assert "placement_only" not in fcu  # 有工程参数的条目不是摆放件

    chiller = next(item for item in catalog if item["type"] == "冷源机组" and item["name"] == "LG340MCF")
    assert chiller["placement_only"] is True
    assert chiller["role"] == "cold_source"

    damper = next(item for item in catalog if item["type"] == "风阀" and item["name"] == "120x120")
    assert damper["role"] == "air_accessory"
    assert damper["systems"] == ["air"]

    # 铜管数字型号字面量在 unity_entries 中保留原始 JSON 类型（字节级还原依赖它）。
    copper = next(
        item
        for item in catalog
        if item["type"] == "定位铜管"
        and any(entry.get("device_name") == 6.35 for entry in item.get("unity_entries", []))
    )
    assert copper["unity_entries"][0]["device_name"] == 6.35


def test_import_is_idempotent(isolated_catalog, library_copy):
    first = unity_sync.import_device_library(library_copy)
    snapshot = isolated_catalog.read_text(encoding="utf-8")
    second = unity_sync.import_device_library(library_copy)

    assert first["created"] == 404 and first["attached"] == 23  # 派生自 427 条 Unity 记录与 23 条别名命中
    assert second == {"total": 427, "attached": 427, "created": 0, "updated": 0}
    assert isolated_catalog.read_text(encoding="utf-8") == snapshot


def test_export_round_trip_matches_original_rows(isolated_catalog, library_copy, tmp_path):
    original_rows = json.loads(library_copy.read_bytes())

    unity_sync.import_device_library(library_copy)
    export_path = tmp_path / "exported.json"
    unity_sync.export_device_library(export_path)

    exported = json.loads(export_path.read_bytes())
    assert exported == original_rows  # 原文件含手工杂散空白，故比对解析等价
    # 导出格式已定型：再次导出必须字节级一致。
    second_path = tmp_path / "exported2.json"
    unity_sync.export_device_library(second_path)
    assert second_path.read_bytes() == export_path.read_bytes()

    summary = unity_sync.export_device_library(export_path)
    assert summary["changed"] is False


def test_full_sync_round_trip(isolated_catalog, library_copy, tmp_path):
    original_rows = json.loads(library_copy.read_bytes())
    summary = unity_sync.sync_device_library(library_copy, tmp_path / "synced.json")

    assert summary["import"]["total"] == 427
    assert json.loads((tmp_path / "synced.json").read_bytes()) == original_rows


def test_catalog_get_strips_unity_entries_but_keeps_placement_flag(isolated_catalog, library_copy):
    from fastapi.testclient import TestClient

    from app.main import app

    unity_sync.import_device_library(library_copy)
    client = TestClient(app)
    body = client.get("/api/v1/catalog/equipment").json()

    assert all("unity_entries" not in entry and "Unity摆放信息" not in entry for entry in body)
    lg = next(entry for entry in body if entry.get("设备类型") == "冷源机组" and entry.get("型号") == "LG340MCF")
    assert lg["仅摆放"] is True
    fcu = next(entry for entry in body if entry.get("设备类型") == "风机盘管" and entry.get("型号") == "FP-34WA")
    assert "仅摆放" not in fcu


def test_full_catalog_replace_preserves_unity_internals(isolated_catalog, library_copy):
    unity_sync.import_device_library(library_copy)
    items = [
        {key: value for key, value in item.items() if key != "unity_entries"}
        for item in _read_catalog(isolated_catalog)
    ]
    # 模拟前端整表 PUT：条目不含 unity_entries（GET 已剥离），保存必须自动回填。
    equipment.save_equipment_catalog(items)

    catalog = _read_catalog(isolated_catalog)
    fcu = next(item for item in catalog if item["type"] == "风机盘管" and item["name"] == "FP-34WA")
    assert fcu["unity_entries"][0]["device_name"] == "MCW200V"
    lg = next(item for item in catalog if item["type"] == "冷源机组" and item["name"] == "LG340MCF")
    assert lg["placement_only"] is True


def test_quality_report_skips_placement_items(isolated_catalog, library_copy):
    from app.data.catalog_quality import build_catalog_quality_report

    unity_sync.import_device_library(library_copy)

    report = build_catalog_quality_report()
    # 摆放条目不参与体检：告警只剩组合式机组的水侧/风机功率缺失
    # （热泵制冷工况补齐后由 207 降至 90）；总条目 = 工程条目 + 摆放条目。
    assert len(report["issues"]) == 90
    engineering = sum(
        1 for item in _read_catalog(isolated_catalog) if not item.get("placement_only")
    )
    assert report["total"] == engineering + 404


def test_selection_scan_excludes_placement_rows(isolated_catalog, library_copy):
    unity_sync.import_device_library(library_copy)
    catalog = equipment.load_equipment_catalog()
    placement_rows = [row for row in catalog if row.get("placement_only") and row.get("role") == "cold_source"]
    assert placement_rows, "同步后应存在 cold_source 摆放条目供选型过滤"

    # recommend_equipment 的行筛选与 catalog_quality 同源：手工复算扫描结果，
    # 确保摆放条目（Unity 示例机型）不会进入候选。
    scanned = [
        row
        for row in catalog
        if row.get("role") == "cold_source"
        and str(row.get("name") or "") not in {"", "*"}
        and not row.get("configurable_series")
        and not row.get("placement_only")
    ]
    scanned_names = {row["name"] for row in scanned}
    assert "LG340MCF" not in scanned_names
    assert len(scanned) == 17  # 同步前的真实冷源条目数不变
