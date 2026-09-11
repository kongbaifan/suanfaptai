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


# Magnus-Tetens 式的有效温区。超出此范围指数项会溢出，且远离空调工况，
# 取值只需保证不抛异常、结果单调即可。
MIN_SATURATION_TEMP_C = -100.0
MAX_SATURATION_TEMP_C = 200.0


def saturation_pressure_pa(temperature_c: float) -> float:
    """饱和水蒸气压

    采用 Magnus-Tetens 式，0~50℃ 内相对误差在 0.1% 量级。
    温度先钳到有效温区：盘管在极小风量/极大冷量下会算出远离工况的假想
    温度，若不设边界，指数项会溢出（`math range error`）。
    """
    if not math.isfinite(temperature_c):
        return 0.0
    clamped = min(max(temperature_c, MIN_SATURATION_TEMP_C), MAX_SATURATION_TEMP_C)
    return 610.94 * math.exp(
        17.625 * clamped / (clamped + 243.04)
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


# 干空气定压比热与饱和水蒸气比热，kJ/(kg·K)
DRY_AIR_CP_KJ_KG_K = 1.006
VAPOR_CP_KJ_KG_K = 1.86
# 0℃ 水蒸气汽化潜热，kJ/kg
VAPORIZATION_HEAT_KJ_KG = 2501.0


def saturation_humidity_ratio(
    temperature_c: float, pressure_pa: float = STANDARD_PRESSURE_PA
) -> float:
    """饱和含湿量（相对湿度 100% 时的含湿量）"""
    return humidity_ratio_from_rh(temperature_c, 100.0, pressure_pa)


def enthalpy_kj_kg(
    temperature_c: float,
    humidity_ratio: float,
    pressure_pa: float = STANDARD_PRESSURE_PA,
) -> float:
    """湿空气比焓（以 0℃ 干空气为基准）

    `h = c_pa·T + w·(r0 + c_pv·T)`，与 `LATENT_HEAT_KJ_KG` 的简化式不同，
    这里保留水蒸气显热项，湿盘管出口温度反解才自洽。
    """
    ratio = max(humidity_ratio, MIN_HUMIDITY_RATIO)
    return (
        DRY_AIR_CP_KJ_KG_K * temperature_c
        + ratio * (VAPORIZATION_HEAT_KJ_KG + VAPOR_CP_KJ_KG_K * temperature_c)
    )


def saturated_enthalpy_kj_kg(
    temperature_c: float, pressure_pa: float = STANDARD_PRESSURE_PA
) -> float:
    """饱和线上某温度的比焓"""
    return enthalpy_kj_kg(
        temperature_c, saturation_humidity_ratio(temperature_c, pressure_pa), pressure_pa
    )


def vapor_enthalpy_kj_kg(
    temperature_c: float,
    humidity_ratio: float,
) -> float:
    """含湿量对应的水蒸气焓 `w·(r0 + c_pv·T)`

    与 `enthalpy_kj_kg` 用同一组常数，因此
    `cp·ΔT + Δvapor_enthalpy` 恰好等于 `Δenthalpy`——把盘管冷量拆成
    显热与潜热时用它可以做到严格守恒，不会因两套潜热常数不同而漏能量。
    """
    ratio = max(humidity_ratio, MIN_HUMIDITY_RATIO)
    return ratio * (VAPORIZATION_HEAT_KJ_KG + VAPOR_CP_KJ_KG_K * temperature_c)


def temperature_from_saturated_enthalpy(
    enthalpy: float,
    pressure_pa: float = STANDARD_PRESSURE_PA,
    *,
    low_c: float = -20.0,
    high_c: float = 60.0,
    iterations: int = 60,
) -> float:
    """湿盘管出口温度：饱和线上按比焓反解

    饱和比焓随温度单调递增，二分即可稳定收敛；步数取 60 次远高于
    双精度所需，代价可忽略。
    """
    if not math.isfinite(enthalpy):
        return low_c
    if enthalpy <= saturated_enthalpy_kj_kg(low_c, pressure_pa):
        return low_c
    if enthalpy >= saturated_enthalpy_kj_kg(high_c, pressure_pa):
        return high_c
    lo, hi = low_c, high_c
    for _ in range(iterations):
        mid = (lo + hi) / 2.0
        if saturated_enthalpy_kj_kg(mid, pressure_pa) < enthalpy:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0
