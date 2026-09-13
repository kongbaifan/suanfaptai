from collections import Counter, defaultdict
from typing import Any

from app.data.equipment import load_equipment_catalog
from app.schemas import Issue

REQUIRED_BY_ROLE = {
    "cold_source": ["capacity_kw", "cop", "power_kw", "chilled_flow_m3_h", "cooling_flow_m3_h"],
    "heat_source": ["capacity_kw", "flow_m3_h", "power_kw", "cop", "cooling_capacity_kw", "cooling_power_kw", "eer"],
    "pump": ["flow_m3_h", "head_m", "power_kw"],
    "terminal": ["capacity_kw", "airflow_m3_h", "water_flow_m3_h", "fan_power_kw"],
    "cooling_tower": ["capacity_kw", "flow_m3_h", "fan_power_kw", "approach_c"],
    "water_accessory": ["local_resistance_coefficient"],
    "air_accessory": ["air_pressure_drop_pa", "local_resistance_coefficient"],
    "air_terminal": ["recommended_airflow_m3_h", "air_pressure_drop_pa"],
}


def build_catalog_quality_report() -> dict[str, Any]:
    catalog = load_equipment_catalog()
    type_counts = Counter(str(item.get("type", "")) for item in catalog)
    role_counts = Counter(str(item.get("role", "unknown")) for item in catalog)
    field_counts: dict[str, int] = defaultdict(int)
    duplicate_keys = []
    seen = set()
    issues: list[Issue] = []

    for item in catalog:
        key = (str(item.get("type", "")), str(item.get("name", "")))
        if key in seen:
            duplicate_keys.append({"type": key[0], "name": key[1]})
        seen.add(key)
        for field in item:
            field_counts[field] += 1
        # Unity 摆放条目只承载预制体/贴图信息，不参与工程参数体检。
        if not item.get("placement_only"):
            issues.extend(_item_issues(item))

    role_quality = {}
    for role, required_fields in REQUIRED_BY_ROLE.items():
        # 按项目选型的系列条目不含具体规格参数，与 _item_issues 保持一致地排除。
        role_items = [
            item
            for item in catalog
            if item.get("role") == role
            and not item.get("configurable_series")
            and not item.get("placement_only")
        ]
        role_quality[role] = {
            "count": len(role_items),
            "field_coverage": {
                field: round(sum(1 for item in role_items if field in item) / len(role_items), 3) if role_items else 0.0
                for field in required_fields
            },
            "missing_key_fields": [
                field
                for field in required_fields
                if role_items and sum(1 for item in role_items if field in item) < len(role_items)
            ],
        }

    for duplicate in duplicate_keys:
        issues.append(Issue(level="warning", code="CATALOG_DUPLICATE_MODEL", message=f"Duplicate catalog item {duplicate['type']} / {duplicate['name']}."))

    return {
        "total": len(catalog),
        "type_counts": dict(type_counts),
        "role_counts": dict(role_counts),
        "field_counts": dict(sorted(field_counts.items())),
        "role_quality": role_quality,
        "duplicates": duplicate_keys,
        "issues": [issue.model_dump() for issue in _dedupe_issues(issues)],
    }


def _item_issues(item: dict[str, Any]) -> list[Issue]:
    issues: list[Issue] = []
    role = str(item.get("role", "unknown"))
    target = f"{item.get('type', '')}/{item.get('name', '')}"
    if item.get("configurable_series"):
        return issues
    for field in REQUIRED_BY_ROLE.get(role, []):
        if field not in item:
            issues.append(Issue(level="info", code="CATALOG_KEY_FIELD_MISSING", message=f"{target} misses key field {field}.", target=target))
    for field in ("capacity_kw", "flow_m3_h", "head_m", "power_kw", "airflow_m3_h", "cop", "iplv", "efficiency", "cooling_capacity_kw", "cooling_power_kw", "eer"):
        if field in item and float(item.get(field) or 0.0) <= 0:
            issues.append(Issue(level="warning", code="CATALOG_NON_POSITIVE_VALUE", message=f"{target} has non-positive {field}.", target=target))
    if "efficiency" in item and float(item["efficiency"]) > 1:
        issues.append(Issue(level="warning", code="CATALOG_EFFICIENCY_RANGE", message=f"{target} efficiency should be 0-1.", target=target))
    return issues


def _dedupe_issues(issues: list[Issue]) -> list[Issue]:
    result = []
    seen = set()
    for issue in issues:
        key = (issue.level, issue.code, issue.message, issue.target)
        if key not in seen:
            seen.add(key)
            result.append(issue)
    return result
