"""湿空气物性

房间湿度必须以含湿量（kg 水 / kg 干空气）为状态量推进，不能直接对相对湿度做积分。
相对湿度是含湿量与当前温度饱和值的比，温度一变它就跟着变——拿它当状态量，
房间只降温不除湿也会算出湿度下降，与实际相反。

教学口径下压力取标准大气压，不随海拔修正。
"""

from __future__ import annotations

import math


# 标准大气压
STANDARD_PRESSURE_PA = 101325.0

# 水与干空气的分子量比
MOLECULAR_WEIGHT_RATIO = 0.621945

# 水的汽化潜热，取常规空调工况附近的定值
LATENT_HEAT_KJ_KG = 2450.0

# 含湿量物理下界，避免除零与负湿
MIN_HUMIDITY_RATIO = 1e-5


def saturation_pressure_pa(temperature_c: float) -> float:
    """饱和水蒸气压

    采用 Magnus-Tetens 式，0~50℃ 内相对误差在 0.1% 量级。
    """
    if not math.isfinite(temperature_c):
        return 0.0
    return 610.94 * math.exp(
        17.625 * temperature_c / (temperature_c + 243.04)
    )


def humidity_ratio_from_rh(
    temperature_c: float,
    relative_humidity_pct: float,
    pressure_pa: float = STANDARD_PRESSURE_PA,
) -> float:
    """由干球温度与相对湿度求含湿量"""
    saturation = saturation_pressure_pa(temperature_c)
    vapor = saturation * _clamp(relative_humidity_pct, 0.0, 100.0) / 100.0
    # 水蒸气分压不可能达到总压，否则干空气分压为零
    vapor = min(vapor, pressure_pa * 0.99)
    return max(
        MOLECULAR_WEIGHT_RATIO * vapor / (pressure_pa - vapor),
        MIN_HUMIDITY_RATIO,
    )


def rh_from_humidity_ratio(
    temperature_c: float,
    humidity_ratio: float,
    pressure_pa: float = STANDARD_PRESSURE_PA,
) -> float:
    """由干球温度与含湿量求相对湿度

    结果截到 100%：含湿量超过当前温度的饱和值意味着结露，
    教学模型不跟踪凝结水，按饱和处理。
    """
    ratio = max(humidity_ratio, MIN_HUMIDITY_RATIO)
    vapor = pressure_pa * ratio / (MOLECULAR_WEIGHT_RATIO + ratio)
    saturation = saturation_pressure_pa(temperature_c)
    if saturation <= 0.0:
        return 0.0
    return _clamp(vapor / saturation * 100.0, 0.0, 100.0)


def _clamp(value: float, low: float, high: float) -> float:
    if math.isnan(value):
        return low
    return max(low, min(high, value))
