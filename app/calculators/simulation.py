"""Steady-state API projection backed by the shared simulation core."""

from __future__ import annotations

from app.schemas import (
    AirNetworkResult,
    HydraulicNetworkResult,
    Issue,
    LoadResult,
    SteadyStateSimulationResult,
    SystemEvaluateRequest,
)
from app.simcore.project import build_project_layout
from app.simcore.session import SimulationConfig
from app.simcore.steady import solve_steady


def simulate_steady_state(
    request: SystemEvaluateRequest,
    load: LoadResult | None,
    hydraulic: HydraulicNetworkResult | None,
    air: AirNetworkResult | None,
) -> SteadyStateSimulationResult:
    """Run the dynamic core to its design-hour fixed point."""
    del (
        hydraulic,
        air,
    )  # Network results are calculated separately and kept in the response.
    cooling_load_kw = load.adjusted_cooling_kw if load else 0.0
    resolved = build_project_layout(request)
    result = solve_steady(
        resolved.layout,
        SimulationConfig(
            step_seconds=300.0,
            design_cooling_load_kw=cooling_load_kw,
        ),
    )
    snapshot = result.snapshot

    issues: list[Issue] = []
    if cooling_load_kw > snapshot.installed_capacity_kw:
        issues.append(
            Issue(
                level="warning",
                code="SIM_CAPACITY_DEFICIT",
                message="Steady-state simulation predicts insufficient cooling capacity.",
            )
        )
    if not result.converged:
        issues.append(
            Issue(
                level="warning",
                code="SIM_NOT_CONVERGED",
                message="Simulation reached the steady-state iteration limit.",
            )
        )
    if "degraded_parameters" in snapshot.alarms:
        issues.append(
            Issue(
                level="warning",
                code="SIM_DEGRADED_PARAMETERS",
                message="One or more device parameters use an explicit fallback.",
            )
        )

    device_values = [reading.values for reading in snapshot.devices.values()]
    supply_air_values = [
        float(values["supply_air_temp_c"])
        for values in device_values
        if "supply_air_temp_c" in values
    ]
    room_temperatures = [room.temperature_c for room in snapshot.rooms.values()]
    cooling_energy = sum(
        float(values.get("power_kw", 0.0))
        for values in device_values
        if "cop" in values
    )
    pump_energy = sum(
        float(values.get("power_kw", 0.0))
        for values in device_values
        if "speed_ratio" in values
    )
    fan_energy = sum(
        float(values.get("fan_power_kw", values.get("power_kw", 0.0)))
        for values in device_values
        if "fan_speed_ratio" in values or "supply_air_temp_c" in values
    )
    rounded_cooling_energy = round(cooling_energy, 3)
    rounded_pump_energy = round(pump_energy, 3)
    rounded_fan_energy = round(fan_energy, 3)
    total_energy = (
        rounded_cooling_energy + rounded_pump_energy + rounded_fan_energy
    )

    return SteadyStateSimulationResult(
        supply_water_temp_c=round(snapshot.supply_water_temp_c, 3),
        return_water_temp_c=round(snapshot.return_water_temp_c, 3),
        supply_air_temp_c=round(
            sum(supply_air_values) / len(supply_air_values)
            if supply_air_values
            else snapshot.supply_water_temp_c,
            3,
        ),
        estimated_room_temp_c=round(
            sum(room_temperatures) / len(room_temperatures)
            if room_temperatures
            else 26.0,
            3,
        ),
        cooling_energy_kw=rounded_cooling_energy,
        pump_energy_kw=rounded_pump_energy,
        fan_energy_kw=rounded_fan_energy,
        total_energy_kw=round(total_energy, 3),
        issues=issues,
    )
