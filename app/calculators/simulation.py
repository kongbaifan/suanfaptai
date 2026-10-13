"""Steady-state API projection backed by the shared simulation core."""

from __future__ import annotations

import math

from app.calculators.load import calculate_load
from app.schemas import (
    AirNetworkResult,
    HydraulicNetworkResult,
    Issue,
    LoadRequest,
    LoadResult,
    SteadyStateSimulationResult,
    SystemEvaluateRequest,
)
from app.simcore.project import build_project_layout
from app.simcore.room import PERSON_SENSIBLE_KW
from app.simcore.session import SimulationConfig
from app.simcore.steady import solve_steady

# 进入房间显热平衡的设计负荷分项（见 `calculators/load.py` 的 components_kw）。
# `fresh_air_cooling`（新风显热）由承担新风的盘管在机组侧处理，`latent_cooling`
# 走房间湿平衡，二者都不是房间显热得热；人员分项是显热 + 潜热合计，单独按人数折算。
ROOM_SENSIBLE_COMPONENTS = ("lighting", "equipment", "solar", "envelope_cooling")


def design_room_sensible_kw(
    request: SystemEvaluateRequest, load: LoadResult | None
) -> float | None:
    """设计冷负荷中由房间显热得热构成的部分 [kW]

        份额       = Σ_房间(照明 + 设备 + 太阳辐射 + 围护 + 人数 · q_人员显热)
                     / Σ_房间 分项法总冷负荷
        Q_房间显热 = 设计总冷负荷 × 份额

    人员按仿真核同一常数 `PERSON_SENSIBLE_KW` 取显热：负荷计算的 `people`
    分项是 0.12 kW/人的显热 + 潜热合计，而人员潜热已由房间湿平衡按
    `PERSON_LATENT_KW` 单独计入，整份记成显热会把潜热算两次。

    按**份额**而不是直接求和，是因为调用方可能只给总负荷：稳态管网求解器
    （`steady_solver.solver`）传入的 `LoadResult` 没有逐房间分项。份额取自
    请求自身的房间分项，两条路径因此对同一请求给出同一个房间显热；
    同时使用系数与安全系数已含在总负荷里，份额是无量纲比，不再重复乘。

    没有总负荷或没有房间时返回 None，不改写房间得热。
    """
    total_kw = load.adjusted_cooling_kw if load is not None else 0.0
    if total_kw <= 0.0 or not request.rooms:
        return None
    rooms = load.rooms if load is not None and load.rooms else calculate_load(
        LoadRequest(
            rooms=request.rooms,
            floors=request.floors,
            simultaneity_factor=request.simultaneity_factor,
            safety_factor=request.safety_factor,
        )
    ).rooms
    people_by_room = {room.id: room.people for room in request.rooms}
    # fsum 与求和顺序无关：房间数组重排不得改变结果（见 test_room_order_invariance）
    sensible_kw = math.fsum(
        [room.components_kw.get(key, 0.0) for room in rooms for key in ROOM_SENSIBLE_COMPONENTS]
        + [people_by_room.get(room.id, 0) * PERSON_SENSIBLE_KW for room in rooms]
    )
    itemized_kw = math.fsum(room.cooling_kw for room in rooms)
    if itemized_kw <= 0.0:
        return None
    return total_kw * min(sensible_kw / itemized_kw, 1.0)


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
            design_room_sensible_kw=design_room_sensible_kw(request, load),
        ),
    )
    snapshot = result.snapshot

    issues: list[Issue] = list(snapshot.network.issues) if snapshot.network else []
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
