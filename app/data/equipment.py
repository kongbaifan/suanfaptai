import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.schemas import DeviceInput

CATALOG_PATH = Path(__file__).with_name("equipment_catalog.json")
NUMERIC_CATALOG_FIELDS = {
    "capacity_kw",
    "flow_m3_h",
    "head_m",
    "power_kw",
    "pressure_drop_pa",
    "airflow_m3_h",
    "cop",
    "iplv",
    "chilled_flow_m3_h",
    "cooling_flow_m3_h",
    "chilled_pressure_drop_kpa",
    "cooling_pressure_drop_kpa",
    "efficiency",
    "rpm",
    "npsh_m",
    "approach_c",
    "fan_power_kw",
    "noise_db",
    "water_flow_m3_h",
    "water_pressure_drop_kpa",
    "external_static_pressure_pa",
    "local_resistance_coefficient",
    "air_pressure_drop_pa",
    "recommended_airflow_m3_h",
    "unit_price",
    "weight_kg",
    "heating_capacity_kw",
    "heating_power_kw",
    "cooling_capacity_kw",
    "cooling_power_kw",
    "eer",
    "source_flow_m3_h",
    "nominal_capacity_ton",
    "airflow_min_m3_h",
    "airflow_max_m3_h",
    "motor_efficiency",
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
}
BOOLEAN_CATALOG_FIELDS = {"water_cooled", "variable_speed", "configurable_series", "placement_only"}
# Unity DeviceLibrary.json 的摆放字段（device_id/model_id/order 为数值，其余为字符串）。
UNITY_ENTRY_NUMERIC_FIELDS = ("device_id", "model_id", "order")
UNITY_ENTRY_TEXT_FIELDS = ("device_name", "position", "position_offset", "image", "prefab_path", "type", "library")


@lru_cache(maxsize=1)
def load_equipment_catalog() -> list[dict[str, Any]]:
    with CATALOG_PATH.open("r", encoding="utf-8") as file:
        data = json.load(file)
    return list(data)


def save_equipment_catalog(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized = [_normalize_catalog_item(item) for item in items]
    _restore_unity_internals(normalized)
    with CATALOG_PATH.open("w", encoding="utf-8") as file:
        json.dump(normalized, file, ensure_ascii=False, indent=2)
        file.write("\n")
    load_equipment_catalog.cache_clear()
    return load_equipment_catalog()


def _restore_unity_internals(items: list[dict[str, Any]]) -> None:
    """保存前把 Unity 摆放字段补回到缺失的条目上。

    GET 出口会剥离 `unity_entries`（内部实现细节），Excel 导入和整表替换的
    条目因此不带该字段；按 (type, name) 对齐磁盘上的旧条目回填，避免整表
    保存把同步得到的摆放信息冲掉。
    """
    existing = {
        (str(item.get("type", "")), str(item.get("name", ""))): item
        for item in load_equipment_catalog()
    }
    for item in items:
        if item.get("unity_entries"):
            continue
        prior = existing.get((str(item.get("type", "")), str(item.get("name", ""))))
        if prior and prior.get("unity_entries"):
            item["unity_entries"] = prior["unity_entries"]
            if "placement_only" not in item and prior.get("placement_only"):
                item["placement_only"] = True


def strip_unity_internals(item: dict[str, Any]) -> dict[str, Any]:
    """GET 出口用：隐藏 unity_entries 内部结构，placement_only 保留供前端过滤。"""
    return {key: value for key, value in item.items() if key != "unity_entries"}


def upsert_equipment_item(item: dict[str, Any]) -> dict[str, Any]:
    normalized = _normalize_catalog_item(item)
    catalog = load_equipment_catalog()
    updated = False
    for index, existing in enumerate(catalog):
        if existing.get("type") == normalized.get("type") and existing.get("name") == normalized.get("name"):
            catalog[index] = normalized
            updated = True
            break
    if not updated:
        catalog.append(normalized)
    save_equipment_catalog(catalog)
    return normalized


def delete_equipment_item(device_type: str, name: str) -> bool:
    catalog = load_equipment_catalog()
    filtered = [item for item in catalog if not (item.get("type") == device_type and str(item.get("name")) == str(name))]
    if len(filtered) == len(catalog):
        return False
    save_equipment_catalog(filtered)
    return True


def merge_equipment_catalog(items: list[dict[str, Any]], replace: bool = False) -> list[dict[str, Any]]:
    if replace:
        return save_equipment_catalog(items)

    merged: dict[tuple[str, str], dict[str, Any]] = {
        (str(item.get("type", "")), str(item.get("name", ""))): item for item in load_equipment_catalog()
    }
    for item in items:
        normalized = _normalize_catalog_item(item)
        merged[(str(normalized.get("type", "")), str(normalized.get("name", "")))] = normalized
    return save_equipment_catalog(list(merged.values()))


# 历史类型名 -> 现名。类型改名后，此前保存的项目仍带旧类型名，解析前统一归一，
# 避免老项目静默退化成内置兜底参数。
LEGACY_DEVICE_TYPE_ALIASES = {
    "一体式新风机组": "新风机组",
}


def resolve_device_type(device_type: str | None) -> str:
    """把历史类型名归一为目录中的现名；未知类型原样返回。

    先 `strip()` 再查别名表。设备类型在界面上是**自由文本**输入框（不是下拉），
    尾随/前导空格是常见输入；目录里没有任何带空格的类型名（已核对全部各行），
    所以剥离空白只会让 `" 新风机组 "` 这类值与 `"新风机组"` 等价，
    不会误命中别的类型。

    这一条同时是前端 `canonicalDeviceType` 的对照口径：前端也 trim 后查表。
    后端是**权威**，前端镜像同一规则，两端对同一输入必须给出同一判定——
    此前，后端不 trim，于是 `" 一体式新风机组 "` 被前端接受、却被后端拒绝
    （`ROOM_BINDING_UNSUPPORTED_DEVICE`），与原缺陷同类的假分歧。
    """
    text = str(device_type or "").strip()
    return LEGACY_DEVICE_TYPE_ALIASES.get(text, text)


def _match_catalog_row(rows: list[dict[str, Any]], device_type: str, name: str | None) -> dict[str, Any] | None:
    """在给定条目集合内按 精确名 -> 别名 -> "*" 兜底 的顺序取首个命中。"""
    if name:
        for item in rows:
            if item.get("type") == device_type and str(item.get("name")) == str(name):
                return item
        for item in rows:
            aliases = {str(alias) for alias in item.get("aliases", [])}
            if item.get("type") == device_type and str(name) in aliases:
                return item
    for item in rows:
        if item.get("type") == device_type and item.get("name") == "*":
            return item
    return None


def find_equipment_spec(device_type: str, name: str | None = None) -> dict[str, Any] | None:
    """工程参数解析入口：只看真实设备条目。

    placement_only 条目只承载 Unity 摆放信息，不带任何额定参数，命中它们会让
    容量/流量/扬程静默变成 0，必须跳过并继续走 "*" 类型默认兜底。
    只需要拓扑角色时用 `topology_spec_for_device`。
    """
    device_type = resolve_device_type(device_type)
    engineering = [item for item in load_equipment_catalog() if not item.get("placement_only")]
    return _match_catalog_row(engineering, device_type, name)


def spec_for_device(device: DeviceInput) -> dict[str, Any] | None:
    return find_equipment_spec(device.type, device.name)


def topology_spec_for_device(device: DeviceInput) -> dict[str, Any] | None:
    """拓扑信息解析入口：工程条目全部落空后，允许回退到 placement_only 条目。

    `role` / `systems` 是纯拓扑声明，placement_only 条目同样带着它们，且部分
    类型（如 `冷源机组`、`冷却塔`）没有 "*" 兜底行——只查工程条目会让这些型号
    的角色变成 None，系统里就"没有冷源"，水力路径枚举随之返回空。
    """
    spec = spec_for_device(device)
    if spec is not None:
        return spec
    placement = [item for item in load_equipment_catalog() if item.get("placement_only")]
    return _match_catalog_row(placement, resolve_device_type(device.type), device.name)


def role_for_device(device: DeviceInput) -> str | None:
    spec = topology_spec_for_device(device)
    return str(spec.get("role")) if spec and spec.get("role") else None


def systems_for_device(device: DeviceInput) -> set[str]:
    spec = topology_spec_for_device(device)
    if not spec:
        return set()
    return {str(system) for system in spec.get("systems", [])}


def capacity_for_device(device: DeviceInput) -> float:
    if device.capacity_kw is not None:
        return device.capacity_kw * device.quantity
    spec = spec_for_device(device)
    if not spec:
        return 0.0
    return float(spec.get("capacity_kw", 0.0)) * device.quantity


def flow_for_device(device: DeviceInput, system: str | None = None) -> float:
    if device.flow_m3_h is not None:
        return device.flow_m3_h * device.quantity
    spec = spec_for_device(device)
    if not spec:
        return 0.0

    fields_by_system = {
        "chilled_water": ("chilled_flow_m3_h", "water_flow_m3_h", "flow_m3_h"),
        "cooling_water": ("cooling_flow_m3_h", "flow_m3_h"),
        "hot_water": ("water_flow_m3_h", "flow_m3_h"),
    }
    field_candidates = (
        fields_by_system.get(system, ("flow_m3_h",))
        if system is not None
        else ("flow_m3_h",)
    )
    for field in field_candidates:
        try:
            value = float(spec.get(field, 0.0) or 0.0)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value * device.quantity
    return 0.0


def head_for_device(device: DeviceInput) -> float:
    if device.head_m is not None:
        return device.head_m
    spec = spec_for_device(device)
    if not spec:
        return 0.0
    return float(spec.get("head_m", 0.0))


def power_for_device(device: DeviceInput) -> float:
    if device.power_kw is not None:
        return device.power_kw * device.quantity
    spec = spec_for_device(device)
    if not spec:
        return 0.0
    return float(spec.get("power_kw", 0.0)) * device.quantity


def fan_power_for_device(device: DeviceInput) -> float:
    spec = spec_for_device(device)
    if not spec:
        return 0.0
    if "fan_power_kw" in spec:
        return float(spec.get("fan_power_kw", 0.0)) * device.quantity
    return float(spec.get("power_kw", 0.0)) * device.quantity


def airflow_for_device(device: DeviceInput) -> float:
    spec = spec_for_device(device)
    if not spec:
        return 0.0
    return float(spec.get("airflow_m3_h", 0.0)) * device.quantity


def catalog_number_for_device(device: DeviceInput, field: str, default: float = 0.0) -> float:
    spec = spec_for_device(device)
    if not spec:
        return default
    value = spec.get(field, default)
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def weighted_average_for_role(devices: list[DeviceInput], role: str, field: str, default: float) -> float:
    weighted_sum = 0.0
    weight_total = 0.0
    for device in devices:
        if role_for_device(device) != role:
            continue
        weight = capacity_for_device(device) or 1.0
        value = catalog_number_for_device(device, field, 0.0)
        if value <= 0:
            continue
        weighted_sum += value * weight
        weight_total += weight
    return weighted_sum / weight_total if weight_total > 0 else default


def is_water_cooled(device: DeviceInput) -> bool:
    spec = spec_for_device(device)
    return bool(spec and spec.get("water_cooled"))


def _normalize_unity_entries(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    entries: list[dict[str, Any]] = []
    for raw in value:
        if not isinstance(raw, dict) or raw.get("device_id") is None:
            continue
        entry: dict[str, Any] = {}
        for key in UNITY_ENTRY_NUMERIC_FIELDS:
            if raw.get(key) is not None:
                entry[key] = int(raw[key])
        for key in UNITY_ENTRY_TEXT_FIELDS:
            if raw.get(key) is not None:
                value = raw[key]
                # device_name 保留 JSON 数字字面量（铜管外径），其余归一为字符串。
                if key == "device_name" and isinstance(value, (int, float)) and not isinstance(value, bool):
                    entry[key] = value
                else:
                    entry[key] = str(value)
        entries.append(entry)
    return entries


def _normalize_catalog_item(item: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(item)
    normalized["type"] = str(normalized.get("type", "")).strip()
    normalized["name"] = str(normalized.get("name", "*")).strip() or "*"
    normalized["role"] = str(normalized.get("role", "unknown")).strip() or "unknown"
    aliases = normalized.get("aliases", [])
    if isinstance(aliases, str):
        aliases = [
            part.strip()
            for part in aliases.replace("；", ";").replace(",", ";").split(";")
            if part.strip()
        ]
    normalized["aliases"] = [str(alias).strip() for alias in aliases if str(alias).strip()]
    systems = normalized.get("systems", [])
    if isinstance(systems, str):
        systems = [part.strip() for part in systems.replace("；", ";").replace(",", ";").split(";") if part.strip()]
    normalized["systems"] = systems

    for key in NUMERIC_CATALOG_FIELDS:
        if key in normalized and normalized[key] not in ("", None):
            normalized[key] = float(normalized[key])
        elif key in normalized:
            normalized.pop(key)

    for key in BOOLEAN_CATALOG_FIELDS:
        if key in normalized:
            value = normalized[key]
            if isinstance(value, str):
                normalized[key] = value.strip().lower() in {"1", "true", "yes", "y", "是"}
            else:
                normalized[key] = bool(value)

    if "unity_entries" in normalized:
        entries = _normalize_unity_entries(normalized["unity_entries"])
        if entries:
            normalized["unity_entries"] = entries
        else:
            normalized.pop("unity_entries")

    return normalized
