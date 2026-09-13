"""室外气象模型

教学场景不接逐时气象数据，用日周期正弦近似即可，但湿球温度必须真算——
冷却塔逼近度以湿球为基准，冷凝温度又反过来决定冷机 COP，这条链路上
湿球是起点。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# 日最高温出现的时刻
PEAK_HOUR = 14.0
# 日温度波动幅值（峰谷差的一半）
DEFAULT_SWING_C = 4.0


@dataclass(frozen=True, slots=True)
class Ambient:
    """室外环境状态"""

    dry_bulb_c: float
    wet_bulb_c: float
    relative_humidity_pct: float
    hour_of_day: float


def ambient_at(
    simulation_seconds: float,
    base_temp_c: float = 33.0,
    relative_humidity_pct: float = 70.0,
    swing_c: float = DEFAULT_SWING_C,
    disturbance_c: float = 0.0,
) -> Ambient:
    """按仿真时刻求室外状态

    干球温度按日周期余弦变化，峰值出现在 `PEAK_HOUR`。

    必须用余弦而非正弦：正弦在自变量为零处取零而非取一，会把峰值推迟 6 小时，
    并让 14:00 与 02:00 算出同一个值。稳态求解以 `PEAK_HOUR` 为设计工况，
    相位错了设计条件就偏软。
    """
    hour = (simulation_seconds / 3600.0) % 24.0
    dry_bulb = base_temp_c + swing_c * math.cos(
        (hour - PEAK_HOUR) / 24.0 * 2.0 * math.pi
    )
    dry_bulb += disturbance_c
    humidity = _clamp(relative_humidity_pct, 5.0, 99.0)
    return Ambient(
        dry_bulb_c=dry_bulb,
        wet_bulb_c=wet_bulb_c(dry_bulb, humidity),
        relative_humidity_pct=humidity,
        hour_of_day=hour,
    )


def wet_bulb_c(dry_bulb_c: float, relative_humidity_pct: float) -> float:
    """由干球温度与相对湿度求湿球温度

    采用 Stull(2011) 经验式，适用范围约 5%~99% RH、-20~50℃，
    在常规空调工况下误差在 ±0.3℃ 量级，对教学演示足够。
    """
    temperature = dry_bulb_c
    humidity = _clamp(relative_humidity_pct, 5.0, 99.0)

    wet_bulb = (
        temperature * math.atan(0.151977 * math.sqrt(humidity + 8.313659))
        + math.atan(temperature + humidity)
        - math.atan(humidity - 1.676331)
        + 0.00391838 * humidity**1.5 * math.atan(0.023101 * humidity)
        - 4.686035
    )
    # 湿球不可能高于干球
    return min(wet_bulb, temperature)


def _clamp(value: float, low: float, high: float) -> float:
    if math.isnan(value):
        return low
    return max(low, min(high, value))
