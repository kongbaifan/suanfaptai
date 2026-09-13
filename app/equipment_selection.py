"""Read-only, deterministic teaching equipment selection."""

from __future__ import annotations

import math
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from app.calculators.air import calculate_air
from app.calculators.hydraulic_network import calculate_hydraulic_network
from app.calculators.load import calculate_load
from app.data.equipment import load_equipment_catalog
from app.data.project_store import get_project
from app.data.rules import rule_value
from app.schemas import (
    AirRequest,
    LoadRequest,
    ProjectRecord,
    SelectionAppliedRules,
    SelectionCandidate,
    SelectionMargins,
    SelectionProvidedValues,
    SelectionRatedValues,
    SelectionReason,
    SelectionRecommendationResult,
    SelectionRequirements,
    SelectionRuleRange,
    SelectionStatus,
    SelectionTargetRole,
)

_EPSILON = 1e-9
_BLOCKING_REASON_CODES = {
    "MISSING_RATED_PARAMETER",
    "CAPACITY_MARGIN_LOW",
    "CHILLED_WATER_FLOW_LOW",
    "HOT_WATER_FLOW_LOW",
    "FLOW_MARGIN_LOW",
    "HEAD_MARGIN_LOW",
    "WATER_FLOW_LOW",
    "AIRFLOW_LOW",
}


@dataclass(frozen=True, slots=True)
class _SelectionRules:
    capacity_min: float
    capacity_max: float
    pump_flow_min: float
    pump_flow_max: float
    pump_head_min: float
    pump_head_max: float

    def response(self) -> SelectionAppliedRules:
        return SelectionAppliedRules(
            capacity_margin=SelectionRuleRange(
                min=_rounded(self.capacity_min), max=_rounded(self.capacity_max)
            ),
            pump_flow_margin=SelectionRuleRange(
                min=_rounded(self.pump_flow_min), max=_rounded(self.pump_flow_max)
            ),
            pump_head_margin=SelectionRuleRange(
                min=_rounded(self.pump_head_min), max=_rounded(self.pump_head_max)
            ),
        )


@dataclass(frozen=True, slots=True)
class _RankedCandidate:
    candidate: SelectionCandidate
    normalized_distance: float
    overage: float


class SelectionRequirementsUnavailableError(ValueError):
    def __init__(
        self, target_role: SelectionTargetRole, missing: list[str]
    ) -> None:
        self.target_role = target_role
        self.missing = tuple(missing)
        super().__init__(
            "Required project data is unavailable for equipment selection."
        )

    @property
    def detail(self) -> dict[str, str | list[str]]:
        return {
            "code": "SELECTION_REQUIREMENTS_UNAVAILABLE",
            "target_role": self.target_role,
            "missing": list(self.missing),
            "message": str(self),
        }


def recommend_equipment(
    project_id: str, target_role: SelectionTargetRole
) -> SelectionRecommendationResult:
    record = get_project(project_id)
    requirements = _derive_requirements(record)
    _require_available(record, target_role, requirements)
    rules = _selection_rules()
    catalog = deepcopy(load_equipment_catalog())
    rows = [
        row
        for row in catalog
        if row.get("role") == target_role
        and _catalog_text(row.get("name")) not in {"", "*"}
        and not row.get("configurable_series")
        and not row.get("placement_only")
    ]

    ranked = [
        _evaluate_candidate(row, target_role, requirements, rules) for row in rows
    ]
    ranked.sort(key=_ranking_key)
    candidates = []
    for rank, item in enumerate(ranked, start=1):
        candidates.append(item.candidate.model_copy(update={"rank": rank}))

    status: SelectionStatus
    if not candidates:
        status = "no_catalog_candidates"
    elif any(candidate.eligible for candidate in candidates):
        status = "recommended"
    else:
        status = "no_eligible_candidates"

    return SelectionRecommendationResult(
        project_id=project_id,
        target_role=target_role,
        status=status,
        requirements=_rounded_requirements(requirements),
        applied_rules=rules.response(),
        candidates=candidates,
    )


def _derive_requirements(record: ProjectRecord) -> SelectionRequirements:
    project = record.project.model_copy(deep=True)
    load = calculate_load(
        LoadRequest(
            rooms=[room.model_copy(deep=True) for room in project.rooms],
            floors=[floor.model_copy(deep=True) for floor in project.floors],
            simultaneity_factor=project.simultaneity_factor,
            safety_factor=project.safety_factor,
        )
    )
    cooling_load_kw = _finite_nonnegative(load.adjusted_cooling_kw)
    heating_load_kw = _finite_nonnegative(load.adjusted_heating_kw)
    hydraulic = calculate_hydraulic_network(
        project,
        cooling_load_kw=cooling_load_kw,
        heating_load_kw=heating_load_kw,
    )
    chilled_flow = _finite_nonnegative(
        hydraulic.total_flow_by_system_m3_h.get("chilled_water", 0.0)
    )
    pump_head = _finite_nonnegative(hydraulic.required_pump_head_m)
    hot_water_flow = _finite_nonnegative(
        hydraulic.total_flow_by_system_m3_h.get("hot_water", 0.0)
    )
    airflow = 0.0
    if cooling_load_kw > 0.0:
        airflow = _finite_nonnegative(
            calculate_air(AirRequest(cooling_load_kw=cooling_load_kw)).airflow_m3_h
        )
    return SelectionRequirements(
        cooling_load_kw=cooling_load_kw,
        heating_load_kw=heating_load_kw,
        chilled_water_flow_m3_h=chilled_flow,
        hot_water_flow_m3_h=hot_water_flow,
        required_pump_head_m=pump_head,
        supply_airflow_m3_h=airflow,
    )


def _require_available(
    record: ProjectRecord,
    target_role: SelectionTargetRole,
    requirements: SelectionRequirements,
) -> None:
    if not record.project.rooms:
        raise SelectionRequirementsUnavailableError(target_role, ["rooms"])

    required_fields = {
        "cold_source": ("cooling_load_kw", "chilled_water_flow_m3_h"),
        "heat_source": ("heating_load_kw", "hot_water_flow_m3_h"),
        "pump": ("chilled_water_flow_m3_h", "required_pump_head_m"),
        "terminal": (
            "cooling_load_kw",
            "chilled_water_flow_m3_h",
            "supply_airflow_m3_h",
        ),
    }[target_role]
    missing = [
        field for field in required_fields if getattr(requirements, field) <= 0.0
    ]
    if missing:
        raise SelectionRequirementsUnavailableError(target_role, missing)


def _selection_rules() -> _SelectionRules:
    values = {
        "capacity_min": _rule_number("capacity_margin.min"),
        "capacity_max": _rule_number("capacity_margin.max"),
        "pump_flow_min": _rule_number("pump_margin.flow_min"),
        "pump_flow_max": _rule_number("pump_margin.flow_max"),
        "pump_head_min": _rule_number("pump_margin.head_min"),
        "pump_head_max": _rule_number("pump_margin.head_max"),
    }
    for prefix in ("capacity", "pump_flow", "pump_head"):
        if values[f"{prefix}_min"] > values[f"{prefix}_max"]:
            raise ValueError(f"Invalid selection rule range: {prefix}.")
    return _SelectionRules(**values)


def _rule_number(path: str) -> float:
    value = rule_value(path)
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Selection rule is missing or invalid: {path}.") from exc
    if not math.isfinite(number) or number <= 0.0:
        raise ValueError(f"Selection rule is missing or invalid: {path}.")
    return number


def _evaluate_candidate(
    row: dict[str, Any],
    target_role: SelectionTargetRole,
    requirements: SelectionRequirements,
    rules: _SelectionRules,
) -> _RankedCandidate:
    evaluators: dict[
        SelectionTargetRole,
        Callable[
            [dict[str, Any], SelectionRequirements, _SelectionRules],
            _RankedCandidate,
        ],
    ] = {
        "cold_source": _evaluate_cold_source,
        "heat_source": _evaluate_heat_source,
        "pump": _evaluate_pump,
        "terminal": _evaluate_terminal,
    }
    return evaluators[target_role](row, requirements, rules)


def _evaluate_cold_source(
    row: dict[str, Any],
    requirements: SelectionRequirements,
    rules: _SelectionRules,
) -> _RankedCandidate:
    rated = _rated_values(row, "cold_source")
    reasons = _missing_reasons(
        (
            ("capacity_kw", rated.capacity_kw),
            ("chilled_water_flow_m3_h", rated.chilled_water_flow_m3_h),
        )
    )
    quantity = _quantity_for(
        requirements.cooling_load_kw * rules.capacity_min, rated.capacity_kw
    )
    provided = _provided_values(rated, quantity, keep_head_per_unit=False)
    capacity_margin = _margin(provided.capacity_kw, requirements.cooling_load_kw)
    chilled_margin = _margin(
        provided.chilled_water_flow_m3_h,
        requirements.chilled_water_flow_m3_h,
    )
    _append_range_reason(
        reasons,
        capacity_margin,
        rules.capacity_min,
        rules.capacity_max,
        "CAPACITY_MARGIN_LOW",
        "CAPACITY_MARGIN_HIGH",
        "总容量裕量低于推荐范围。",
        "总容量裕量高于推荐范围。",
    )
    _append_lower_reason(
        reasons,
        chilled_margin,
        1.0,
        "CHILLED_WATER_FLOW_LOW",
        "总冷冻水流量低于派生需求。",
    )
    margins = SelectionMargins(
        capacity=_rounded_optional(capacity_margin),
        chilled_water_flow=_rounded_optional(chilled_margin),
    )
    distance = _range_distance(
        capacity_margin, rules.capacity_min, rules.capacity_max
    ) + _lower_distance(chilled_margin, 1.0)
    overage = _overage(capacity_margin, rules.capacity_min) + _overage(
        chilled_margin, 1.0
    )
    return _ranked_candidate(row, "cold_source", quantity, rated, provided, margins, reasons, distance, overage)


def _evaluate_heat_source(
    row: dict[str, Any],
    requirements: SelectionRequirements,
    rules: _SelectionRules,
) -> _RankedCandidate:
    rated = _rated_values(row, "heat_source")
    reasons = _missing_reasons(
        (("capacity_kw", rated.capacity_kw), ("flow_m3_h", rated.flow_m3_h))
    )
    quantity = max(
        _quantity_for(
            requirements.heating_load_kw * rules.capacity_min,
            rated.capacity_kw,
        ),
        _quantity_for(requirements.hot_water_flow_m3_h, rated.flow_m3_h),
    )
    provided = _provided_values(rated, quantity, keep_head_per_unit=False)
    capacity_margin = _margin(provided.capacity_kw, requirements.heating_load_kw)
    hot_water_margin = _margin(
        provided.flow_m3_h, requirements.hot_water_flow_m3_h
    )
    _append_range_reason(
        reasons,
        capacity_margin,
        rules.capacity_min,
        rules.capacity_max,
        "CAPACITY_MARGIN_LOW",
        "CAPACITY_MARGIN_HIGH",
        "总制热量裕量低于推荐范围。",
        "总制热量裕量高于推荐范围。",
    )
    _append_lower_reason(
        reasons,
        hot_water_margin,
        1.0,
        "HOT_WATER_FLOW_LOW",
        "总热水流量低于派生需求。",
    )
    margins = SelectionMargins(
        capacity=_rounded_optional(capacity_margin),
        hot_water_flow=_rounded_optional(hot_water_margin),
    )
    distance = _range_distance(
        capacity_margin, rules.capacity_min, rules.capacity_max
    ) + _lower_distance(hot_water_margin, 1.0)
    overage = _overage(capacity_margin, rules.capacity_min) + _overage(
        hot_water_margin, 1.0
    )
    return _ranked_candidate(
        row,
        "heat_source",
        quantity,
        rated,
        provided,
        margins,
        reasons,
        distance,
        overage,
    )


def _evaluate_pump(
    row: dict[str, Any],
    requirements: SelectionRequirements,
    rules: _SelectionRules,
) -> _RankedCandidate:
    rated = _rated_values(row, "pump")
    reasons = _missing_reasons(
        (("flow_m3_h", rated.flow_m3_h), ("head_m", rated.head_m))
    )
    quantity = _quantity_for(
        requirements.chilled_water_flow_m3_h * rules.pump_flow_min,
        rated.flow_m3_h,
    )
    provided = _provided_values(rated, quantity, keep_head_per_unit=True)
    flow_margin = _margin(
        provided.flow_m3_h, requirements.chilled_water_flow_m3_h
    )
    head_margin = _margin(provided.head_m, requirements.required_pump_head_m)
    _append_range_reason(
        reasons,
        flow_margin,
        rules.pump_flow_min,
        rules.pump_flow_max,
        "FLOW_MARGIN_LOW",
        "FLOW_MARGIN_HIGH",
        "总流量裕量低于推荐范围。",
        "总流量裕量高于推荐范围。",
    )
    _append_range_reason(
        reasons,
        head_margin,
        rules.pump_head_min,
        rules.pump_head_max,
        "HEAD_MARGIN_LOW",
        "HEAD_MARGIN_HIGH",
        "单泵扬程裕量低于推荐范围。",
        "单泵扬程裕量高于推荐范围。",
    )
    margins = SelectionMargins(
        flow=_rounded_optional(flow_margin), head=_rounded_optional(head_margin)
    )
    distance = _range_distance(
        flow_margin, rules.pump_flow_min, rules.pump_flow_max
    ) + _range_distance(
        head_margin, rules.pump_head_min, rules.pump_head_max
    )
    overage = _overage(flow_margin, rules.pump_flow_min) + _overage(
        head_margin, rules.pump_head_min
    )
    return _ranked_candidate(row, "pump", quantity, rated, provided, margins, reasons, distance, overage)


def _evaluate_terminal(
    row: dict[str, Any],
    requirements: SelectionRequirements,
    rules: _SelectionRules,
) -> _RankedCandidate:
    rated = _rated_values(row, "terminal")
    reasons = _missing_reasons(
        (
            ("capacity_kw", rated.capacity_kw),
            ("water_flow_m3_h", rated.water_flow_m3_h),
            ("airflow_m3_h", rated.airflow_m3_h),
        )
    )
    quantities = (
        _quantity_for(
            requirements.cooling_load_kw * rules.capacity_min,
            rated.capacity_kw,
        ),
        _quantity_for(
            requirements.chilled_water_flow_m3_h, rated.water_flow_m3_h
        ),
        _quantity_for(requirements.supply_airflow_m3_h, rated.airflow_m3_h),
    )
    quantity = max(quantities)
    provided = _provided_values(rated, quantity, keep_head_per_unit=False)
    capacity_margin = _margin(provided.capacity_kw, requirements.cooling_load_kw)
    water_margin = _margin(
        provided.water_flow_m3_h, requirements.chilled_water_flow_m3_h
    )
    airflow_margin = _margin(
        provided.airflow_m3_h, requirements.supply_airflow_m3_h
    )
    _append_range_reason(
        reasons,
        capacity_margin,
        rules.capacity_min,
        rules.capacity_max,
        "CAPACITY_MARGIN_LOW",
        "CAPACITY_MARGIN_HIGH",
        "总容量裕量低于推荐范围。",
        "总容量裕量高于推荐范围。",
    )
    _append_lower_reason(
        reasons,
        water_margin,
        1.0,
        "WATER_FLOW_LOW",
        "总水流量低于派生需求。",
    )
    _append_lower_reason(
        reasons,
        airflow_margin,
        1.0,
        "AIRFLOW_LOW",
        "总送风量低于派生需求。",
    )
    margins = SelectionMargins(
        capacity=_rounded_optional(capacity_margin),
        airflow=_rounded_optional(airflow_margin),
        water_flow=_rounded_optional(water_margin),
    )
    distance = (
        _range_distance(capacity_margin, rules.capacity_min, rules.capacity_max)
        + _lower_distance(water_margin, 1.0)
        + _lower_distance(airflow_margin, 1.0)
    )
    overage = (
        _overage(capacity_margin, rules.capacity_min)
        + _overage(water_margin, 1.0)
        + _overage(airflow_margin, 1.0)
    )
    return _ranked_candidate(row, "terminal", quantity, rated, provided, margins, reasons, distance, overage)


def _rated_values(
    row: dict[str, Any], role: SelectionTargetRole
) -> SelectionRatedValues:
    if role == "cold_source":
        return SelectionRatedValues(
            capacity_kw=_positive_number(row.get("capacity_kw")),
            chilled_water_flow_m3_h=_positive_number(
                row.get("chilled_flow_m3_h")
            ),
            power_kw=_positive_number(row.get("power_kw")),
            cop=_positive_number(row.get("cop")),
        )
    if role == "heat_source":
        return SelectionRatedValues(
            capacity_kw=_positive_number(row.get("capacity_kw")),
            flow_m3_h=_positive_number(row.get("flow_m3_h")),
            power_kw=_positive_number(row.get("power_kw")),
            cop=_positive_number(row.get("cop")),
        )
    if role == "pump":
        return SelectionRatedValues(
            flow_m3_h=_positive_number(row.get("flow_m3_h")),
            head_m=_positive_number(row.get("head_m")),
            power_kw=_positive_number(row.get("power_kw")),
        )
    return SelectionRatedValues(
        capacity_kw=_positive_number(row.get("capacity_kw")),
        airflow_m3_h=_positive_number(row.get("airflow_m3_h")),
        water_flow_m3_h=_positive_number(row.get("water_flow_m3_h")),
        power_kw=_positive_number(row.get("fan_power_kw")),
    )


def _provided_values(
    rated: SelectionRatedValues, quantity: int, *, keep_head_per_unit: bool
) -> SelectionProvidedValues:
    return SelectionProvidedValues(
        capacity_kw=_scaled(rated.capacity_kw, quantity),
        chilled_water_flow_m3_h=_scaled(
            rated.chilled_water_flow_m3_h, quantity
        ),
        flow_m3_h=_scaled(rated.flow_m3_h, quantity),
        head_m=rated.head_m if keep_head_per_unit else _scaled(rated.head_m, quantity),
        airflow_m3_h=_scaled(rated.airflow_m3_h, quantity),
        water_flow_m3_h=_scaled(rated.water_flow_m3_h, quantity),
        power_kw=_scaled(rated.power_kw, quantity),
    )


def _ranked_candidate(
    row: dict[str, Any],
    role: SelectionTargetRole,
    quantity: int,
    rated: SelectionRatedValues,
    provided: SelectionProvidedValues,
    margins: SelectionMargins,
    reasons: list[SelectionReason],
    distance: float,
    overage: float,
) -> _RankedCandidate:
    candidate = SelectionCandidate(
        rank=1,
        device_type=_catalog_text(row.get("type")),
        model_name=_catalog_text(row.get("name")),
        role=role,
        quantity=quantity,
        rated=_rounded_rated_values(rated),
        provided=_rounded_provided_values(provided),
        margins=margins,
        eligible=not any(
            reason.code in _BLOCKING_REASON_CODES for reason in reasons
        ),
        reasons=reasons,
    )
    return _RankedCandidate(candidate, distance, overage)


def _ranking_key(item: _RankedCandidate) -> tuple[Any, ...]:
    candidate = item.candidate
    return (
        not candidate.eligible,
        len(candidate.reasons),
        item.normalized_distance,
        item.overage,
        candidate.device_type,
        candidate.model_name,
    )


def _missing_reasons(
    fields: tuple[tuple[str, float | None], ...]
) -> list[SelectionReason]:
    return [
        SelectionReason(
            code="MISSING_RATED_PARAMETER",
            message=f"目录缺少有效的正数额定参数：{field}。",
        )
        for field, value in fields
        if value is None
    ]


def _append_range_reason(
    reasons: list[SelectionReason],
    value: float | None,
    lower: float,
    upper: float,
    low_code: str,
    high_code: str,
    low_message: str,
    high_message: str,
) -> None:
    if value is None:
        return
    if _below(value, lower):
        reasons.append(SelectionReason(code=low_code, message=low_message))
    elif _above(value, upper):
        reasons.append(SelectionReason(code=high_code, message=high_message))


def _append_lower_reason(
    reasons: list[SelectionReason],
    value: float | None,
    lower: float,
    code: str,
    message: str,
) -> None:
    if value is not None and _below(value, lower):
        reasons.append(SelectionReason(code=code, message=message))


def _quantity_for(required: float, rated: float | None) -> int:
    if rated is None:
        return 1
    ratio = required / rated
    nearest_integer = round(ratio)
    if math.isclose(
        ratio, nearest_integer, rel_tol=_EPSILON, abs_tol=_EPSILON
    ):
        return max(1, nearest_integer)
    return max(1, math.ceil(ratio))


def _margin(provided: float | None, required: float) -> float | None:
    if provided is None or required <= 0.0:
        return None
    return provided / required


def _range_distance(
    value: float | None, lower: float, upper: float
) -> float:
    if value is None:
        return 1.0
    width = upper - lower
    if _below(value, lower):
        return (lower - value) / width if width > 0.0 else lower - value
    if _above(value, upper):
        return (value - upper) / width if width > 0.0 else value - upper
    return 0.0


def _lower_distance(value: float | None, lower: float) -> float:
    if value is None:
        return 1.0
    return max(0.0, lower - value) / lower


def _overage(value: float | None, lower: float) -> float:
    return max(0.0, value - lower) if value is not None else 0.0


def _below(value: float, lower: float) -> bool:
    return value < lower and not math.isclose(
        value, lower, rel_tol=_EPSILON, abs_tol=_EPSILON
    )


def _above(value: float, upper: float) -> bool:
    return value > upper and not math.isclose(
        value, upper, rel_tol=_EPSILON, abs_tol=_EPSILON
    )


def _positive_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0.0 else None


def _catalog_text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _finite_nonnegative(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("Derived selection requirement is not numeric.") from exc
    if not math.isfinite(number) or number < 0.0:
        raise ValueError("Derived selection requirement must be finite and nonnegative.")
    return number


def _scaled(value: float | None, quantity: int) -> float | None:
    return value * quantity if value is not None else None


def _rounded_rated_values(
    values: SelectionRatedValues,
) -> SelectionRatedValues:
    return SelectionRatedValues(
        capacity_kw=_rounded_optional(values.capacity_kw),
        chilled_water_flow_m3_h=_rounded_optional(
            values.chilled_water_flow_m3_h
        ),
        flow_m3_h=_rounded_optional(values.flow_m3_h),
        head_m=_rounded_optional(values.head_m),
        airflow_m3_h=_rounded_optional(values.airflow_m3_h),
        water_flow_m3_h=_rounded_optional(values.water_flow_m3_h),
        power_kw=_rounded_optional(values.power_kw),
        cop=_rounded_optional(values.cop),
    )


def _rounded_provided_values(
    values: SelectionProvidedValues,
) -> SelectionProvidedValues:
    return SelectionProvidedValues(
        capacity_kw=_rounded_optional(values.capacity_kw),
        chilled_water_flow_m3_h=_rounded_optional(
            values.chilled_water_flow_m3_h
        ),
        flow_m3_h=_rounded_optional(values.flow_m3_h),
        head_m=_rounded_optional(values.head_m),
        airflow_m3_h=_rounded_optional(values.airflow_m3_h),
        water_flow_m3_h=_rounded_optional(values.water_flow_m3_h),
        power_kw=_rounded_optional(values.power_kw),
    )


def _rounded_optional(value: float | None) -> float | None:
    return _rounded(value) if value is not None else None


def _rounded_requirements(
    requirements: SelectionRequirements,
) -> SelectionRequirements:
    return SelectionRequirements(
        cooling_load_kw=_rounded(requirements.cooling_load_kw),
        heating_load_kw=_rounded(requirements.heating_load_kw),
        chilled_water_flow_m3_h=_rounded(
            requirements.chilled_water_flow_m3_h
        ),
        hot_water_flow_m3_h=_rounded(requirements.hot_water_flow_m3_h),
        required_pump_head_m=_rounded(requirements.required_pump_head_m),
        supply_airflow_m3_h=_rounded(requirements.supply_airflow_m3_h),
    )


def _rounded(value: float) -> float:
    return round(value, 3)
