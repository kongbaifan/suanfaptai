"""设备目录与 Unity DeviceLibrary.json 的双向同步。

设备库的唯一数据源是 `app/data/equipment_catalog.json`。Unity 客户端运行时
读取的 `DeviceLibrary.json`（摆放字段：DeviceID/ModelID/预制体/贴图等）从主
库导出生成，不再手工维护：

- `import_device_library`：把 DeviceLibrary.json 的条目并入主库。型号名命中
  主库型号或别名的（风机盘管 MCW 系列、冷却塔 CT 系列等）挂到原条目的
  `unity_entries`；其余创建 `placement_only` 摆放条目（管件、阀件、Unity 示例
  机型），不带工程参数。
- `export_device_library`：把主库所有 `unity_entries` 按原始顺序还原成
  DeviceLibrary.json，字节级稳定，Unity 场景引用的 (DeviceID, ModelID)
  稳定键不受影响。

角色映射复用 `app/unity_catalog/roles.py` 的 DEVICE_TYPE_TO_ROLE /
ROLE_TO_SYSTEM，再折算成主库的粗粒度角色词表。
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from app.data.equipment import load_equipment_catalog, save_equipment_catalog
from app.release import resource_path
from app.unity_catalog.roles import (
    DEVICE_TYPE_TO_ROLE,
    ROLE_TO_SYSTEM,
    DeviceRole,
    SystemType,
)

DEVICE_LIBRARY_RELATIVE = "Assets/StreamingAssets/SoftwareSerializationData/Json/DeviceLibrary.json"

# Unity 行字段 -> unity_entries 内部字段（导出时按原顺序还原）。
_UNITY_ROW_FIELDS = (
    ("DeviceID", "device_id"),
    ("ModelID", "model_id"),
    ("DeviceName", "device_name"),
    ("DevicePosition", "position"),
    ("DevicePositionOffset", "position_offset"),
    ("DeviceImage", "image"),
    ("DevicePrefabPath", "prefab_path"),
    ("DeviceType", "type"),
    ("DeviceLibrary", "library"),
)

# Unity 细粒度角色 -> 主库粗粒度角色词表（见 app/data/catalog_quality.py）。
_UNITY_ROLE_TO_CATALOG_ROLE: dict[str, str] = {
    DeviceRole.CHILLER.value: "cold_source",
    DeviceRole.HEAT_SOURCE.value: "heat_source",
    DeviceRole.PUMP.value: "pump",
    DeviceRole.PUMP_CHILLED_WATER.value: "pump",
    DeviceRole.PUMP_COOLING_WATER.value: "pump",
    DeviceRole.COOLING_TOWER.value: "cooling_tower",
    DeviceRole.FCU.value: "terminal",
    DeviceRole.AHU.value: "terminal",
    DeviceRole.FRESH_AIR_UNIT.value: "terminal",
    DeviceRole.WATER_VALVE.value: "water_accessory",
    DeviceRole.MANIFOLD.value: "water_accessory",
    DeviceRole.AIR_VALVE.value: "air_accessory",
    DeviceRole.FIRE_DAMPER.value: "air_accessory",
    DeviceRole.AIR_OUTLET.value: "air_terminal",
    DeviceRole.PIPE_STRAIGHT.value: "pipe",
    DeviceRole.PIPE_TEE.value: "pipe",
    DeviceRole.PIPE_CROSS.value: "pipe",
    DeviceRole.PIPE_REDUCER.value: "pipe",
    DeviceRole.PIPE_BRANCH.value: "pipe",
    DeviceRole.MEASUREMENT.value: "measurement",
}

_WATER_SYSTEMS = ["chilled_water", "cooling_water", "hot_water"]
_ROLE_TO_CATALOG_SYSTEMS: dict[str, list[str]] = {
    SystemType.WATER.value: _WATER_SYSTEMS,
    SystemType.AIR.value: ["air"],
    SystemType.BOTH.value: [*_WATER_SYSTEMS, "air"],
}


def device_library_path() -> Path:
    return resource_path(DEVICE_LIBRARY_RELATIVE)


def import_device_library(library_path: Path | None = None) -> dict[str, Any]:
    """把 DeviceLibrary.json 并入主库，返回 {total, attached, created, updated}。"""
    path = library_path or device_library_path()
    with path.open("r", encoding="utf-8") as file:
        library = json.load(file)

    catalog = load_equipment_catalog()
    by_key = {(str(item.get("type", "")), str(item.get("name", ""))): item for item in catalog}

    attached = 0
    created = 0
    updated = 0
    for order, row in enumerate(library):
        entry = _unity_row_to_entry(row, order)
        device_type = str(row.get("DeviceType", ""))
        device_name = str(row.get("DeviceName", ""))
        target = by_key.get((device_type, device_name))
        if target is None:
            target = _find_by_alias(catalog, device_type, device_name)
        if target is None:
            target = _create_placement_item(device_type, device_name, entry)
            catalog.append(target)
            by_key[(device_type, device_name)] = target
            created += 1
        else:
            if _upsert_entry(target, entry):
                updated += 1
            attached += 1

    save_equipment_catalog(catalog)
    return {"total": len(library), "attached": attached, "created": created, "updated": updated}


def _find_by_alias(catalog: list[dict[str, Any]], device_type: str, device_name: str) -> dict[str, Any] | None:
    for item in catalog:
        if str(item.get("type", "")) != device_type:
            continue
        if device_name in {str(alias) for alias in item.get("aliases", [])}:
            return item
    return None


def _create_placement_item(device_type: str, device_name: str, entry: dict[str, Any]) -> dict[str, Any]:
    role = _catalog_role(device_type)
    return {
        "type": device_type,
        "name": device_name,
        "aliases": [],
        "role": role,
        "systems": _ROLE_TO_CATALOG_SYSTEMS.get(_role_system(device_type), _WATER_SYSTEMS),
        "brand": "",
        "series": "",
        "placement_only": True,
        "unity_entries": [entry],
    }


def _catalog_role(device_type: str) -> str:
    unity_role = DEVICE_TYPE_TO_ROLE.get(device_type)
    if unity_role is None:
        return "unknown"
    return _UNITY_ROLE_TO_CATALOG_ROLE.get(unity_role.value, "unknown")


def _role_system(device_type: str) -> str:
    unity_role = DEVICE_TYPE_TO_ROLE.get(device_type)
    if unity_role is None:
        return SystemType.WATER.value
    return ROLE_TO_SYSTEM.get(unity_role, SystemType.WATER).value


def _unity_row_to_entry(row: dict[str, Any], order: int) -> dict[str, Any]:
    entry: dict[str, Any] = {"order": order}
    for row_key, entry_key in _UNITY_ROW_FIELDS:
        value = row.get(row_key)
        if value is None:
            continue
        if entry_key in ("device_id", "model_id"):
            entry[entry_key] = int(value)
        else:
            # DeviceName 可能是 JSON 数字字面量（铜管外径 6.35 等），保留原类型
            # 才能字节级还原；主库条目 name 仍按规范存字符串。
            entry[entry_key] = value
    return entry


def _entry_to_unity_row(entry: dict[str, Any]) -> dict[str, Any]:
    row: dict[str, Any] = {}
    for row_key, entry_key in _UNITY_ROW_FIELDS:
        value = entry.get(entry_key)
        if value is None:
            continue
        row[row_key] = value
    return row


def _upsert_entry(item: dict[str, Any], entry: dict[str, Any]) -> bool:
    """按 (device_id, model_id) 覆盖条目内容；返回是否有实际变化。"""
    entries = [dict(e) for e in item.setdefault("unity_entries", []) if isinstance(e, dict)]
    key = (entry.get("device_id"), entry.get("model_id"))
    changed = False
    for index, existing in enumerate(entries):
        if (existing.get("device_id"), existing.get("model_id")) == key:
            changed = existing != entry
            entries[index] = entry
            break
    else:
        entries.append(entry)
        changed = True
    entries.sort(key=lambda e: e.get("order", 10**9))
    if entries:
        item["unity_entries"] = entries
    return changed


def export_device_library(target_path: Path | None = None) -> dict[str, Any]:
    """从主库导出 DeviceLibrary.json（字节级稳定），返回 {count, changed, paths}。"""
    entries: list[dict[str, Any]] = []
    for item in load_equipment_catalog():
        for entry in item.get("unity_entries", []):
            if isinstance(entry, dict):
                entries.append(entry)
    entries.sort(key=lambda e: e.get("order", 10**9))
    payload = [_entry_to_unity_row(entry) for entry in entries]
    # 原文件为 CRLF、两空格缩进、无结尾换行；逐字节保持一致，避免 Unity 工程产生无谓 diff。
    data = json.dumps(payload, ensure_ascii=False, indent=2).replace("\n", "\r\n").encode("utf-8")

    if target_path is not None:
        targets = [target_path]
    else:
        # 本文件在 app/data/ 下：parents[2] 是 suanfaptaizhichi 项目根，
        # parents[3] 是 Unity 工程根（两份 Assets 拷贝都要回写）。
        project_root = Path(__file__).resolve().parents[2]
        local = project_root / DEVICE_LIBRARY_RELATIVE
        targets = [local]
        sibling = project_root.parent / DEVICE_LIBRARY_RELATIVE
        if sibling != local and sibling.exists():
            targets.append(sibling)

    changed = False
    written: list[str] = []
    for target in targets:
        if target.exists() and target.read_bytes() == data:
            written.append(str(target))
            continue
        _atomic_write(target, data)
        changed = True
        written.append(str(target))
    return {"count": len(payload), "changed": changed, "paths": written}


def _atomic_write(target: Path, data: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, tmp_name = tempfile.mkstemp(dir=target.parent, suffix=".tmp")
    try:
        with os.fdopen(handle, "wb") as file:
            file.write(data)
        os.replace(tmp_name, target)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def sync_device_library(library_path: Path | None = None, target_path: Path | None = None) -> dict[str, Any]:
    imported = import_device_library(library_path)
    exported = export_device_library(target_path)
    return {"import": imported, "export": exported}
