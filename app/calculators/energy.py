import math

from app.calculators.load import calculate_load
from app.data.equipment import (
    capacity_for_device,
    fan_power_for_device,
    power_for_device,
    role_for_device,
    weighted_average_for_role,
)
from app.schemas import (
    EnergyHourlyPoint,
    EnergySimulationRequest,
    EnergySimulationResult,
    Issue,
    LoadRequest,
)


def simulate_energy(request: EnergySimulationRequest) -> EnergySimulationResult:
    project = request.project
    load = calculate_load(
        LoadRequest(
            rooms=project.rooms,
            floors=project.floors,
            simultaneity_factor=project.simultaneity_factor,
            safety_factor=project.safety_factor,
        )
    ) if project.rooms else None

    peak_load_kw = load.adjusted_cooling_kw if load else 0.0
    cold_capacity_kw = sum(capacity_for_device(device) for device in project.devices if role_for_device(device) == "cold_source")
    pump_power_kw = sum(power_for_device(device) for device in project.devices if role_for_device(device) == "pump")
    fan_power_kw = sum(fan_power_for_device(device) for device in project.devices if role_for_device(device) in {"terminal", "cooling_tower"})
    chiller_cop = weighted_average_for_role(project.devices, "cold_source", "cop", request.chiller_cop)
    average_pump_efficiency = weighted_average_for_role(project.devices, "pump", "efficiency", 0.72)
    issues: list[Issue] = []

    if peak_load_kw > 0 and cold_capacity_kw and cold_capacity_kw < peak_load_kw:
        issues.append(Issue(level="warning", code="ENERGY_CAPACITY_DEFICIT", message="Peak load is higher than installed cold-source capacity."))

    hourly: list[EnergyHourlyPoint] = []
    for hour in range(request.hours):
        hour_of_day = hour % 24
        seasonal_factor = _seasonal_factor(hour, request.hours)
        occupancy_factor = _occupancy_factor(hour_of_day, request.occupancy_start, request.occupancy_end)
        load_factor = max(0.08, seasonal_factor * occupancy_factor)
        load_kw = peak_load_kw * load_factor
        part_load_ratio = min(load_kw / cold_capacity_kw, 1.2) if cold_capacity_kw > 0 else (1.0 if load_kw > 0 else 0.0)
        cooling_energy_kw = load_kw / chiller_cop if chiller_cop > 0 else 0.0
        pump_efficiency_factor = 0.72 / average_pump_efficiency if average_pump_efficiency > 0 else 1.0
        pump_energy_kw = pump_power_kw * pump_efficiency_factor * (0.35 + 0.65 * min(part_load_ratio, 1.0) ** 2) if pump_power_kw > 0 and load_kw > 0 else 0.0
        fan_energy_kw = fan_power_kw * (0.30 + 0.70 * min(part_load_ratio, 1.0) ** 3) if fan_power_kw > 0 and load_kw > 0 else 0.0
        total_energy_kw = cooling_energy_kw + pump_energy_kw + fan_energy_kw
        hourly.append(
            EnergyHourlyPoint(
                hour=hour,
                load_kw=round(load_kw, 3),
                part_load_ratio=round(part_load_ratio, 3),
                cooling_energy_kw=round(cooling_energy_kw, 3),
                pump_energy_kw=round(pump_energy_kw, 3),
                fan_energy_kw=round(fan_energy_kw, 3),
                total_energy_kw=round(total_energy_kw, 3),
            )
        )

    cooling_energy_kwh = sum(point.cooling_energy_kw for point in hourly)
    pump_energy_kwh = sum(point.pump_energy_kw for point in hourly)
    fan_energy_kwh = sum(point.fan_energy_kw for point in hourly)
    total_energy_kwh = cooling_energy_kwh + pump_energy_kwh + fan_energy_kwh

    return EnergySimulationResult(
        hours=request.hours,
        peak_load_kw=round(peak_load_kw, 3),
        peak_energy_kw=round(max((point.total_energy_kw for point in hourly), default=0.0), 3),
        cooling_energy_kwh=round(cooling_energy_kwh, 3),
        pump_energy_kwh=round(pump_energy_kwh, 3),
        fan_energy_kwh=round(fan_energy_kwh, 3),
        total_energy_kwh=round(total_energy_kwh, 3),
        estimated_cost=round(total_energy_kwh * request.electricity_price_per_kwh, 2),
        hourly=hourly,
        issues=issues,
    )


def _seasonal_factor(hour: int, total_hours: int) -> float:
    if total_hours <= 24:
        return 1.0
    position = hour / max(total_hours - 1, 1)
    return 0.45 + 0.55 * (0.5 + 0.5 * math.sin(2 * math.pi * position - math.pi / 2))


def _occupancy_factor(hour_of_day: int, start: int, end: int) -> float:
    if start <= hour_of_day < end:
        midday = (start + end) / 2
        span = max((end - start) / 2, 1)
        shape = 1.0 - min(abs(hour_of_day - midday) / span, 1.0) * 0.25
        return shape
    return 0.28
