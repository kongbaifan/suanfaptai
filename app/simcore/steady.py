"""稳态求解

稳态不是独立的第二套物理，而是动态核的不动点：反复推进 `SimulationSession`
直到室温和活动水回路温度不再变化，取收敛后的快照。这样 REST 的设计工况评估与 WS 的实时推送
共用同一套设备模型与热平衡，两条链路不可能算出不一致的结果。

必须有显式迭代上限——参数异常（例如冷量远超负荷持续过冲）时可能不收敛，
不能让请求线程挂死。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace

from app.simcore.session import (
    PlantLayout,
    PlantSnapshot,
    SimulationConfig,
    SimulationSession,
)
from app.simcore.weather import PEAK_HOUR

DEFAULT_MAX_TICKS = 500
DEFAULT_TOLERANCE_C = 0.01
# 设计工况取日最高温时刻
DESIGN_HOUR_OF_DAY = PEAK_HOUR


@dataclass(frozen=True, slots=True)
class SteadyResult:
    """稳态结果

    `converged` 为假时 `snapshot` 仍然可用，但代表的是达到迭代上限时的状态，
    调用方应把它当作警告而不是设计依据。
    """

    snapshot: PlantSnapshot
    ticks: int
    converged: bool
    final_drift_c: float


def solve_steady(
    layout: PlantLayout,
    config: SimulationConfig | None = None,
    max_ticks: int = DEFAULT_MAX_TICKS,
    tolerance_c: float = DEFAULT_TOLERANCE_C,
    *,
    initial_controls: Mapping[str, Mapping[str, float]] | None = None,
) -> SteadyResult:
    """推进到房间与活动水回路温度收敛

    收敛判据为所有房间及有实际负荷回路的单步温度变化最大值小于容差。
    """
    if max_ticks <= 0:
        raise ValueError("max_ticks must be positive")
    if tolerance_c <= 0.0:
        raise ValueError("tolerance_c must be positive")

    # 室外气象随时间变化时房间温度会一直跟着摆动，不存在不动点。
    # 设计工况必须把气象冻结在固定时刻。
    config = config or SimulationConfig()
    if config.fixed_hour_of_day is None:
        config = replace(config, fixed_hour_of_day=DESIGN_HOUR_OF_DAY)

    session = SimulationSession(layout, config)
    for instance_id, values in (initial_controls or {}).items():
        session.set_controls(instance_id, values)
    previous = session.snapshot
    snapshot = previous
    drift = float("inf")

    for tick in range(1, max_ticks + 1):
        snapshot = session.step()
        drift = _temperature_drift(previous, snapshot)
        previous = snapshot
        if drift <= tolerance_c:
            return SteadyResult(
                snapshot=snapshot, ticks=tick, converged=True, final_drift_c=drift
            )

    return SteadyResult(
        snapshot=snapshot, ticks=max_ticks, converged=False, final_drift_c=drift
    )


def _temperature_drift(previous: PlantSnapshot, current: PlantSnapshot) -> float:
    differences = [
        abs(reading.temperature_c - previous.rooms[room_id].temperature_c)
        for room_id, reading in current.rooms.items()
        if room_id in previous.rooms
    ]
    if current.cooling_load_kw > 0.0 and current.chilled_flow_m3_h > 0.0:
        differences.extend(
            (
                abs(current.supply_water_temp_c - previous.supply_water_temp_c),
                abs(current.return_water_temp_c - previous.return_water_temp_c),
            )
        )
    if current.heating_load_kw > 0.0 and current.hot_water_flow_m3_h > 0.0:
        differences.extend(
            (
                abs(
                    current.hot_water_supply_temp_c
                    - previous.hot_water_supply_temp_c
                ),
                abs(
                    current.hot_water_return_temp_c
                    - previous.hot_water_return_temp_c
                ),
            )
        )
    # 没有房间和活动水侧负荷时没有动态热状态，一步即达稳态。
    return max(differences, default=0.0)
