import math
from typing import Any

from app.data.standards import STANDARD_RECT_DUCTS, STANDARD_ROUND_DUCTS_MM
from app.schemas import AirRequest, AirResult, Issue

AIR_DENSITY_KG_M3 = 1.2
AIR_CP_J_KG_K = 1005.0


def _area_for_rect(width_mm: float, height_mm: float) -> float:
    return width_mm / 1000.0 * height_mm / 1000.0


def _area_for_round(diameter_mm: float) -> float:
    return math.pi * (diameter_mm / 1000.0) ** 2 / 4.0


def calculate_air(request: AirRequest) -> AirResult:
    airflow_m3_h = request.cooling_load_kw * 1000.0 * 3600.0 / (
        AIR_DENSITY_KG_M3 * AIR_CP_J_KG_K * request.supply_return_delta_t_c
    )
    flow_m3_s = airflow_m3_h / 3600.0
    required_area_m2 = flow_m3_s / request.target_velocity_m_s
    issues: list[Issue] = []

    # 与 AirResult.selected_duct 同为 dict[str, Any]：混入 type 字符串后值类型不再统一。
    selected: dict[str, Any] | None = None
    if request.duct_type == "round":
        for diameter_mm in STANDARD_ROUND_DUCTS_MM:
            area = _area_for_round(diameter_mm)
            if area >= required_area_m2:
                selected = {"type": "round", "diameter_mm": diameter_mm, "area_m2": round(area, 4)}
                break
        if selected is None:
            diameter_mm = STANDARD_ROUND_DUCTS_MM[-1]
            area = _area_for_round(diameter_mm)
            selected = {"type": "round", "diameter_mm": diameter_mm, "area_m2": round(area, 4)}
            issues.append(Issue(level="warning", code="DUCT_OVERSIZE_REQUIRED", message="Required duct area exceeds standard table."))
    else:
        for width_mm, height_mm in STANDARD_RECT_DUCTS:
            area = _area_for_rect(width_mm, height_mm)
            if area >= required_area_m2:
                selected = {"type": "rectangular", "width_mm": width_mm, "height_mm": height_mm, "area_m2": round(area, 4)}
                break
        if selected is None:
            width_mm, height_mm = STANDARD_RECT_DUCTS[-1]
            area = _area_for_rect(width_mm, height_mm)
            selected = {"type": "rectangular", "width_mm": width_mm, "height_mm": height_mm, "area_m2": round(area, 4)}
            issues.append(Issue(level="warning", code="DUCT_OVERSIZE_REQUIRED", message="Required duct area exceeds standard table."))

    actual_velocity_m_s = flow_m3_s / float(selected["area_m2"])
    if actual_velocity_m_s > 8.0:
        issues.append(Issue(level="warning", code="AIR_VELOCITY_HIGH", message="Air velocity is higher than the recommended range."))
    if actual_velocity_m_s < 2.0:
        issues.append(Issue(level="info", code="AIR_VELOCITY_LOW", message="Air velocity is lower than common main duct range."))

    return AirResult(
        airflow_m3_h=round(airflow_m3_h, 3),
        selected_duct=selected,
        actual_velocity_m_s=round(actual_velocity_m_s, 3),
        issues=issues,
    )

