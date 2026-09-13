"""稳态求解

稳态不是独立的第二套物理，而是动态核的不动点：反复推进 `SimulationSession`
直到室温和活动水回路温度不再变化，取收敛后的快照。这样 REST 的设计工况评估与 WS 的实时推送
共用同一套设备模型与热平衡，两条链路不可能算出不一致的结果。

必须有显式迭代上限——参数异常（例如冷量远超负荷持续过冲）时可能不收敛，
不能让请求线程挂死。
"""

from __future__ import annotations

from collections import deque
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
# 收敛判据的窗口长度（tick 数）。
#
# 判据必须在**窗口内**考察**最大单步变化**，而不是与某个固定滞后的状态比较：
#
# 1) 只看单步会误报：极限环的折返点单步变化为零。实测四层样例在自动调节下
#    持续振荡（室温 ±0.9 K、湿度 ±9 个百分点），旧判据在 tick 48 宣布收敛，
#    而该点继续积分漂移 3.34 K。
#
# 2) 与固定滞后比较也不行：周期轨道在任何滞后上都存在相位对齐，
#    实测 W 取 50/84/100/150/200/269 时最小漂移全都会掉到近零，
#    总有一个相位骗过判据。
#
# 3) 窗口内最大单步变化则要求**整个窗口内每步都很小**。极限环的步长不可能
#    处处小（否则它就不会走完一个周期），因此必然被拒。
#
# 实测在 W=100 时两种情形相差约 77 倍：
#   极限环样例：窗口内最大单步变化的最小值 = 0.0285 K  -> 判为未收敛
#   真收敛样例：窗口内最大单步变化的最小值 = 0.00037 K -> 判为收敛
# W=100..200 均可区分；取 100 以兼顾速度。
STEADY_WINDOW_TICKS = 100


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

    收敛判据为：最近 `STEADY_WINDOW_TICKS` 步内，**任何一步**的状态变化都不
    超过容差（即窗口内最大单步漂移 ≤ 容差）。

    为什么是"窗口内最大单步"而不是单步或固定滞后：
    - 单步：极限环折返点变化为零，实测在 tick 48 误报，该点继续积分漂移 3.34 K。
    - 固定滞后 W：周期轨道在该滞后上存在相位对齐，实测 W=50..269 的最小漂移
      都会掉到近零，总有一个相位骗过判据。
    - 窗口内最大单步：要求整个窗口每步都小。极限环不可能步长处处小，否则走不
      完一个周期；而真收敛的系统步长会持续衰减。实测 W=100 时两者相差约 77 倍。
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

    # 保留最近 W 步的**单步漂移**，判据取其中的最大值
    recent_steps: deque[float] = deque(maxlen=STEADY_WINDOW_TICKS)
    previous = session.snapshot
    snapshot = previous
    drift = float("inf")

    for tick in range(1, max_ticks + 1):
        snapshot = session.step()
        step_drift = _temperature_drift(previous, snapshot)
        previous = snapshot
        recent_steps.append(step_drift)

        # 无房间且水侧无活动负荷时没有动态热状态，一步即达稳态
        if tick == 1 and _has_no_dynamic_state(snapshot):
            return SteadyResult(
                snapshot=snapshot, ticks=tick, converged=True, final_drift_c=0.0
            )

        if len(recent_steps) == STEADY_WINDOW_TICKS:
            drift = max(recent_steps)
            if drift <= tolerance_c:
                return SteadyResult(
                    snapshot=snapshot, ticks=tick, converged=True, final_drift_c=drift
                )

    return SteadyResult(
        snapshot=snapshot, ticks=max_ticks, converged=False, final_drift_c=drift
    )


def _has_no_dynamic_state(snapshot: PlantSnapshot) -> bool:
    """快照中是否存在任何动态热状态（房间温度或活动水回路温度）

    没有房间、且冷冻/热水侧都无负荷流量时，`_temperature_drift` 恒为 0，
    系统一步即达稳态——这与"极限环需要足够窗口才能排除"是两回事，
    故不套用窗口等待。
    """
    if snapshot.rooms:
        return False
    if snapshot.cooling_load_kw > 0.0 and snapshot.chilled_flow_m3_h > 0.0:
        return False
    return not (
        snapshot.heating_load_kw > 0.0 and snapshot.hot_water_flow_m3_h > 0.0
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
