import math
from typing import Any

from app.data.rules import rule_value
from app.data.standards import STANDARD_WATER_PIPES
from app.schemas import Issue, WaterRequest, WaterResult


WATER_DENSITY_KG_M3 = 1000.0
GRAVITY_M_S2 = 9.80665
FRICTION_FACTOR = 0.025


def _pipe_area_m2(inner_diameter_mm: float) -> float:
    return math.pi * (inner_diameter_mm / 1000.0) ** 2 / 4.0


def calculate_water(request: WaterRequest) -> WaterResult:
    flow_m3_h = 0.86 * request.load_kw / request.delta_t_c
    return calculate_water_from_flow(
        flow_m3_h=flow_m3_h,
        target_velocity_m_s=request.target_velocity_m_s,
        length_m=request.length_m,
        local_resistance_coefficient=request.local_resistance_coefficient,
    )


def calculate_water_from_flow(
    flow_m3_h: float,
    target_velocity_m_s: float = 1.5,
    length_m: float = 30.0,
    local_resistance_coefficient: float = 6.0,
    diameter_mm: float | None = None,
    system: str = "chilled_water",
) -> WaterResult:
    flow_m3_s = flow_m3_h / 3600.0
    required_area_m2 = flow_m3_s / target_velocity_m_s
    issues: list[Issue] = []

    # 与 WaterResult.selected_pipe 同为 dict[str, Any]：混入 source 字符串后值类型不再统一。
    selected: dict[str, Any] | None = None
    if diameter_mm:
        area = _pipe_area_m2(diameter_mm)
        selected = {"dn": round(diameter_mm), "inner_diameter_mm": diameter_mm, "area_m2": round(area, 6), "source": "input"}
    else:
        for pipe in STANDARD_WATER_PIPES:
            area = _pipe_area_m2(pipe["inner_diameter_mm"])
            if area >= required_area_m2:
                selected = {**pipe, "area_m2": round(area, 6), "source": "standard"}
                break

    if not selected:
        pipe = STANDARD_WATER_PIPES[-1]
        area = _pipe_area_m2(pipe["inner_diameter_mm"])
        selected = {**pipe, "area_m2": round(area, 6), "source": "standard_max"}
        issues.append(Issue(level="warning", code="PIPE_OVERSIZE_REQUIRED", message="Required water pipe area exceeds standard table."))

    actual_velocity_m_s = flow_m3_s / float(selected["area_m2"])
    diameter_m = float(selected["inner_diameter_mm"]) / 1000.0
    dynamic_pressure_pa = WATER_DENSITY_KG_M3 * actual_velocity_m_s**2 / 2.0
    friction_factor = float(rule_value("pressure_defaults.water_friction_factor", FRICTION_FACTOR))
    friction_drop_pa = friction_factor * (length_m / diameter_m) * dynamic_pressure_pa if diameter_m else 0.0
    local_drop_pa = local_resistance_coefficient * dynamic_pressure_pa
    pressure_drop_pa = friction_drop_pa + local_drop_pa
    pump_head_m = pressure_drop_pa / (WATER_DENSITY_KG_M3 * GRAVITY_M_S2) * 1.1

    velocity_rule = rule_value(f"water_velocity_m_s.{system}", {"min": 0.6, "max": 2.5})
    if actual_velocity_m_s > velocity_rule["max"]:
        issues.append(Issue(level="warning", code="WATER_VELOCITY_HIGH", message="Water velocity is higher than the recommended range."))
    if actual_velocity_m_s < velocity_rule["min"]:
        issues.append(Issue(level="info", code="WATER_VELOCITY_LOW", message="Water velocity is lower than common pipe range."))

    return WaterResult(
        flow_m3_h=round(flow_m3_h, 3),
        selected_pipe=selected,
        actual_velocity_m_s=round(actual_velocity_m_s, 3),
        pressure_drop_pa=round(pressure_drop_pa, 3),
        pump_head_m=round(pump_head_m, 3),
        issues=issues,
    )
