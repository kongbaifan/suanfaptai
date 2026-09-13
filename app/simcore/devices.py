"""设备物理模型

所有模型遵循 `实际值 = 额定值 × f(可调参数)`：额定值来自 `RatedSpec`（选型决定，只读），
可调参数来自运行期控制指令。模型刻意保持简单，但必须满足三条约束——
方向正确、量级合理、连续可导（读数跳变在教学演示中比不精确更糟）。

本模块为纯函数，不持有状态。状态与时间步进见 `simcore.session`。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from app.simcore.hydronics import MAX_HYDRONIC_DELTA_T_C, WATER_KW_PER_M3H_K
from app.simcore.psychrometrics import (
    DRY_AIR_CP_KJ_KG_K,
    VAPORIZATION_HEAT_KJ_KG,
    VAPOR_CP_KJ_KG_K,
    enthalpy_kj_kg,
    saturation_humidity_ratio,
    temperature_from_saturated_enthalpy,
)
from app.simcore.rated import RatedSpec
from app.unity_catalog import DeviceRole


# 风侧：Q(kW) = V(m3/h) · ρ · c_p / 3600 · ΔT
AIR_KW_PER_M3H_K = 1.2 * 1.005 / 3600.0
# 空气密度，与 calculators/room 各处口径一致
AIR_DENSITY_KG_M3 = 1.2
# 盘管出风温度的物理下界：低于此值属假想工况（风量过小/冷量过大），
# 钳制后仍满足"等湿/饱和"判据的逻辑一致性，不让数值无意义区进入计算。
MIN_COIL_LEAVING_TEMP_C = -20.0

# 冷机额定工况的冷凝温度基准，冷凝侧偏离此值时修正 COP
CONDENSER_REFERENCE_C = 30.0
# 冷凝温度每升高 1℃ 的 COP 衰减比例
CONDENSER_COP_PENALTY_PER_K = 0.025

# 部分负荷效率峰值所在的负荷率
PEAK_EFFICIENCY_PLR = 0.7
# 无 IPLV 时的默认曲线陡度
DEFAULT_PLR_CURVATURE = 3.0
# AHRI 综合部分负荷权重展开后的系数，用于由 IPLV 反解曲线陡度
_IPLV_CURVATURE_COEFFICIENT = 0.04575
# 部分负荷曲线在极低负荷率处的下界。
#
# 原实现用无下界的抛物线 `peak - α·(PLR-0.7)²`，在 PLR→0 时趋于 -∞。
# SCWE-90H+B（α=3.54）在 PLR=0.097 处 plr_factor 只有 0.0334，COP 被
# `_clamp(cop, 1.5, ...)` 的下界钳到 1.5 —— 该钳位随后掩盖了冷凝温度的影响，
# 使"降低冷却水温/风量 -> 冷凝温度升高 -> COP 下降"这条教学链路在低负荷时
# 完全失效（冷凝温度 30 与 50℃ 算出同一个 COP=1.5）。
#
# 真实冷机在低负荷下通过卸载/间歇运行维持效率，COP 下降但不会崩塌。取 0.45
# 作为曲线下界，使低负荷 COP 仍随冷凝温度变化，保留可观测的教学因果关系。
# 该下界与 `_clamp(cop, 1.5, 12.0)` 共同作用，两者都不再在常规工况内饱和。
PLR_FACTOR_FLOOR = 0.45

# 目录标称容量为全热能力，只有显热部分体现为送风温降
SENSIBLE_HEAT_RATIO = 0.75

# --- 盘管 ε-NTU 模型额定工况 -----------------------------------------------
# **重要：目录未记录额定工况。** 549 条目录项没有任何字段记录进风干湿球或
# 进/出水温度，以下工况是由目录数据反解出来的，属"目录缺工况"的降级假定，
# 显式记录于此，不得静默改变。
#
# 反解依据（进水 7℃、水温差 5 K 由目录 Q = c·W·ΔT 反解得，64 个末端均在
# 5.00 K 附近，偏差 < 0.02 K）：
#   风侧：风机盘管系列显热温差落在 11.85..14.49 K
# 与 GB/T 19232 名义工况（进风干球 27℃、进水 7℃、水温差 5 K）一致。
#
# 进风温度取 27℃ 由 ε<1 这一热力学硬约束强制，不是偏好：
# 单程盘管的 Q 不可能超过 C_min·ΔT 这个上限，故必须 ε = Q/(C_min·ΔT) < 1。
# 逐型号检验 9 个风机盘管（进水 7℃）：
#     进风 25℃ -> 2/9 型号 ε >= 1（FP-34WA 达 1.0731），物理不可达
#     进风 26℃ -> 1/9 型号 ε >= 1（FP-34WA 达 1.0166），物理不可达
#     进风 27℃ -> 0/9 型号越界，ε 落在 0.79..0.97，UA 随容量单调，自洽
# 因此 27℃ 是唯一使整个风机盘管目录同时可标定的进风温度。
#
# 另一条常被误用的判据是"出风干球必须高于出水温度 12℃"。该判据不成立：
# 湿盘管的出风下限是**机器露点**而非出水温度，逆流盘管表面温度从进水 7℃
# 到出水 12℃ 分布，机器露点下界由进水 7℃ 给出。按正确判据检验，9 个型号
# 出风干球最小 11.51℃（进风 26℃ 时），远高于 7℃ 下限，全部通过。
COIL_RATED_AIR_TEMP_C = 27.0
COIL_RATED_WATER_TEMP_C = 7.0


@dataclass(frozen=True, slots=True)
class ChillerState:
    load_ratio: float
    cooling_kw: float
    cop: float
    power_kw: float
    supply_temp_c: float
    return_temp_c: float
    flow_m3_h: float
    heat_reject_kw: float


@dataclass(frozen=True, slots=True)
class HeatSourceState:
    """热泵源侧状态

    `heating_kw` 与 `cooling_kw` 按工况互斥：制热工况 `cooling_kw=0`，
    制冷工况（四通阀换向）`heating_kw=0`。`cop` 口径随工况——制热为
    COP、制冷为 EER 折算值。制冷排热进 `heat_reject_kw`，与冷机同构。
    """

    load_ratio: float
    heating_kw: float
    cop: float
    power_kw: float
    supply_temp_c: float
    return_temp_c: float
    flow_m3_h: float
    cooling_kw: float = 0.0
    heat_reject_kw: float = 0.0


@dataclass(frozen=True, slots=True)
class PumpState:
    speed_ratio: float
    flow_m3_h: float
    head_m: float
    pressure_pa: float
    power_kw: float


@dataclass(frozen=True, slots=True)
class CoolingTowerState:
    fan_speed_ratio: float
    approach_c: float
    entering_water_temp_c: float
    leaving_water_temp_c: float
    flow_m3_h: float
    fan_power_kw: float


@dataclass(frozen=True, slots=True)
class TerminalState:
    valve_opening_ratio: float
    cooling_kw: float
    heating_kw: float
    airflow_m3_h: float
    water_flow_m3_h: float
    supply_air_temp_c: float
    power_kw: float
    # 单程盘管标定不可行时置位（目录容量超过 C_min·ΔT 热力学上限）。
    # 这类末端在额定工况下无法同时满足 Q=目录容量 与 ε<1，实际采用的是
    # 上限容量。按「降级透明」约定必须显式暴露，不得静默钳制。
    # 典型来源：新风机组（全新风工况，盘管负荷远高于同风量回风工况）
    # 与 8 排 AHU（显热比 0.658，SENSIBLE_HEAT_RATIO=0.75 高估显热）。
    coil_calibration_limited: bool = False


@dataclass(frozen=True, slots=True)
class ValveState:
    opening_ratio: float
    resistance_ratio: float


def chiller_state(
    rated: RatedSpec,
    load_kw: float,
    supply_temp_c: float,
    condenser_temp_c: float,
    chilled_flow_m3_h: float | None = None,
    enabled: bool = True,
) -> ChillerState:
    """冷机：部分负荷 COP 曲线 + 冷凝温度修正

    `supply_temp_c` 是可调参数（学生设定的出水温度）；额定容量与额定 COP 由型号锁定。

    冷冻水流量由**水泵**决定而非冷机自身，因此由 `chilled_flow_m3_h` 传入；
    缺省时取型号额定流量作为设计工况参考。回水温由负荷与实际流量反算——
    泵转速降下来，温差就会自然拉大。
    """
    capacity_kw = _positive(rated.capacity_kw, 1.0)
    if not enabled or load_kw <= 0.0:
        return ChillerState(
            load_ratio=0.0,
            cooling_kw=0.0,
            cop=0.0,
            power_kw=0.0,
            supply_temp_c=supply_temp_c,
            return_temp_c=supply_temp_c,
            flow_m3_h=0.0,
            heat_reject_kw=0.0,
        )

    load_ratio = load_kw / capacity_kw
    cooling_kw = min(load_kw, capacity_kw)
    cop = _chiller_cop(rated, min(load_ratio, 1.0), condenser_temp_c)
    power_kw = cooling_kw / cop if cop > 0.0 else 0.0

    flow_m3_h = (
        chilled_flow_m3_h
        if chilled_flow_m3_h is not None
        else _positive(rated.flow_m3_h, 0.0)
    )
    delta_t = water_delta_t(cooling_kw, flow_m3_h)

    return ChillerState(
        load_ratio=load_ratio,
        cooling_kw=cooling_kw,
        cop=cop,
        power_kw=power_kw,
        supply_temp_c=supply_temp_c,
        return_temp_c=supply_temp_c + _clamp(delta_t, 0.0, 20.0),
        flow_m3_h=flow_m3_h,
        heat_reject_kw=cooling_kw + power_kw,
    )


def _chiller_cop(rated: RatedSpec, load_ratio: float, condenser_temp_c: float) -> float:
    """冷机 COP：部分负荷曲线 × 冷凝温度修正

    两者都必须保持对输入的**可观测敏感性**——任一环节被钳位饱和，另一环节的
    影响就会被掩盖，教学链路随即断裂。因此：
    - `PLR_FACTOR_FLOOR` 给抛物线一个物理下界（低负荷卸载），
    - 冷凝修正保持线性，不再被总钳位吞掉。
    """
    rated_cop = _positive(rated.cop, 4.5)
    curvature = _plr_curvature(rated)
    # 曲线在 PEAK_EFFICIENCY_PLR 处取峰值，并归一化为满负荷时 f(1.0) = 1.0
    peak = 1.0 + curvature * (1.0 - PEAK_EFFICIENCY_PLR) ** 2
    plr_factor = peak - curvature * (load_ratio - PEAK_EFFICIENCY_PLR) ** 2
    condenser_factor = 1.0 - CONDENSER_COP_PENALTY_PER_K * (
        condenser_temp_c - CONDENSER_REFERENCE_C
    )
    cop = (
        rated_cop
        * _clamp(plr_factor, PLR_FACTOR_FLOOR, 1.6)
        * _clamp(condenser_factor, 0.4, 1.3)
    )
    return _clamp(cop, 1.5, 12.0)


def _plr_curvature(rated: RatedSpec) -> float:
    """由 IPLV 反解部分负荷曲线陡度

    按 AHRI 权重（100/75/50/25% 对应 1/42/45/12%）展开归一化曲线，
    得到 `IPLV / COP = 1 + 0.04575 · α`，故 α 可由样本参数直接确定。
    目录缺 IPLV 时取默认陡度。
    """
    rated_cop = _positive(rated.cop, 0.0)
    iplv = _positive(rated.iplv, 0.0)
    if rated_cop <= 0.0 or iplv <= 0.0:
        return DEFAULT_PLR_CURVATURE
    curvature = (iplv / rated_cop - 1.0) / _IPLV_CURVATURE_COEFFICIENT
    return _clamp(curvature, 0.0, 6.0)


def heat_source_state(
    rated: RatedSpec,
    load_kw: float,
    supply_temp_c: float,
    outdoor_temp_c: float,
    hot_water_flow_m3_h: float | None = None,
    enabled: bool = True,
) -> HeatSourceState:
    """Heat pump teaching model with bounded part-load and ambient corrections."""
    capacity_kw = _positive(rated.capacity_kw, 1.0)
    flow_m3_h = (
        hot_water_flow_m3_h
        if hot_water_flow_m3_h is not None
        else _positive(rated.flow_m3_h, 0.0)
    )
    if not enabled or load_kw <= 0.0 or flow_m3_h <= 0.0:
        return HeatSourceState(
            load_ratio=0.0,
            heating_kw=0.0,
            cop=0.0,
            power_kw=0.0,
            supply_temp_c=supply_temp_c,
            return_temp_c=supply_temp_c,
            flow_m3_h=0.0,
        )

    heating_kw = min(load_kw, capacity_kw)
    load_ratio = heating_kw / capacity_kw
    part_load_factor = _clamp(0.85 + 0.15 * load_ratio, 0.75, 1.0)
    ambient_factor = 1.0
    if "空气源" in rated.device_type or rated.model_name.upper().startswith("ASHP"):
        ambient_factor = _clamp(1.0 - 0.02 * (7.0 - outdoor_temp_c), 0.65, 1.15)
    cop = _clamp(
        _positive(rated.cop, 3.0) * part_load_factor * ambient_factor,
        1.2,
        8.0,
    )
    delta_t = water_delta_t(heating_kw, flow_m3_h)
    return HeatSourceState(
        load_ratio=load_ratio,
        heating_kw=heating_kw,
        cop=cop,
        power_kw=heating_kw / cop,
        supply_temp_c=supply_temp_c,
        return_temp_c=supply_temp_c - _clamp(delta_t, 0.0, 20.0),
        flow_m3_h=flow_m3_h,
    )


def heat_source_cooling_state(
    rated: RatedSpec,
    load_kw: float,
    supply_temp_c: float,
    outdoor_temp_c: float,
    chilled_flow_m3_h: float | None = None,
    enabled: bool = True,
) -> HeatSourceState:
    """热泵制冷工况：四通阀换向后的教学模型

    与 `heat_source_state` 对称。制冷容量取目录 `capacity_kw`（热泵目录口径
    即铭牌制冷量）；制冷 COP 优先 `cop`（制冷口径），目录缺分工况字段时
    用 `capacity_kw / power_kw` 在线推导（121 条目录兜底，见 R4）。
    空气源制冷随室外温度高于 35°C 基准衰减；水/地源按恒温源近似不修正。

    `heating_kw` 恒为 0：复用 `HeatSourceState` 结构避免双状态类型分裂，
    制冷量经 realtime 读数以 `cooling_kw` 键透出。
    """
    capacity_kw = _positive(rated.capacity_kw, 1.0)
    flow_m3_h = (
        chilled_flow_m3_h
        if chilled_flow_m3_h is not None
        else _positive(rated.flow_m3_h, 0.0)
    )
    if not enabled or load_kw <= 0.0 or flow_m3_h <= 0.0:
        return HeatSourceState(
            load_ratio=0.0,
            heating_kw=0.0,
            cop=0.0,
            power_kw=0.0,
            supply_temp_c=supply_temp_c,
            return_temp_c=supply_temp_c,
            flow_m3_h=0.0,
        )

    cooling_kw = min(load_kw, capacity_kw)
    load_ratio = cooling_kw / capacity_kw
    part_load_factor = _clamp(0.85 + 0.15 * load_ratio, 0.75, 1.0)
    ambient_factor = 1.0
    if "空气源" in rated.device_type or rated.model_name.upper().startswith("ASHP"):
        ambient_factor = _clamp(1.0 - 0.015 * (outdoor_temp_c - 35.0), 0.6, 1.1)
    rated_cooling_cop = _positive(rated.cop, 0.0)
    if rated_cooling_cop <= 0.0:
        # 目录缺分工况字段：制冷 COP = 制冷量 / 额定功率（电功率口径一致）
        rated_power = _positive(rated.power_kw, 0.0)
        if rated_power > 0.0:
            rated_cooling_cop = capacity_kw / rated_power
    cop = _clamp(
        rated_cooling_cop * part_load_factor * ambient_factor
        if rated_cooling_cop > 0.0
        else 3.5,
        1.2,
        8.0,
    )
    delta_t = water_delta_t(cooling_kw, flow_m3_h)
    power_kw = cooling_kw / cop
    return HeatSourceState(
        load_ratio=load_ratio,
        heating_kw=0.0,
        cop=cop,
        power_kw=power_kw,
        supply_temp_c=supply_temp_c,
        return_temp_c=supply_temp_c + _clamp(delta_t, 0.0, 20.0),
        flow_m3_h=flow_m3_h,
        cooling_kw=cooling_kw,
        heat_reject_kw=cooling_kw + power_kw,
    )


def pump_state(
    rated: RatedSpec,
    speed_ratio: float,
    enabled: bool = True,
    actual_flow_m3_h: float | None = None,
) -> PumpState:
    """水泵：相似定律 Q∝n、H∝n²，实际功率 P∝Q·n²

    教学演示主力——降转速会同时改变流量、扬程、轴功率，并通过水环路把
    供回水温差拉大。未传实际流量时保持 P∝n³ 的旧调用语义；额定点
    由型号决定。
    """
    ratio = _clamp(speed_ratio, 0.0, 1.2) if enabled else 0.0
    flow_rated = _positive(rated.flow_m3_h, 0.0)
    head_rated = _positive(rated.head_m, 0.0)
    power_rated = _positive(rated.power_kw, 0.0)

    available_flow = flow_rated * ratio
    if actual_flow_m3_h is None:
        flow_m3_h = available_flow
        power_kw = power_rated * ratio**3
    else:
        finite_flow = actual_flow_m3_h if math.isfinite(actual_flow_m3_h) else 0.0
        flow_m3_h = _clamp(finite_flow, 0.0, available_flow)
        power_kw = (
            power_rated * ratio**2 * flow_m3_h / flow_rated if flow_rated > 0.0 else 0.0
        )

    head_m = head_rated * ratio**2
    return PumpState(
        speed_ratio=ratio,
        flow_m3_h=flow_m3_h,
        head_m=head_m,
        pressure_pa=head_m * 1000.0 * 9.80665,
        power_kw=power_kw,
    )


def cooling_tower_state(
    rated: RatedSpec,
    fan_speed_ratio: float,
    heat_reject_kw: float,
    wetbulb_temp_c: float,
    water_flow_m3_h: float | None = None,
    enabled: bool = True,
) -> CoolingTowerState:
    """冷却塔：逼近度决定冷凝侧进水温

    风机转速下降时逼近度变大，进而抬高冷凝温度、压低冷机 COP——
    这条跨设备的反馈链是教学演示中最有说服力的一段。
    """
    ratio = _clamp(fan_speed_ratio, 0.0, 1.0) if enabled else 0.0
    approach_rated = _positive(rated.approach_c, 5.0)
    # 风机转速越低逼近度越大；分母下限避免转速趋零时发散
    approach_c = approach_rated * (0.6 + 0.4 / max(ratio, 0.15))

    flow_m3_h = (
        water_flow_m3_h
        if water_flow_m3_h is not None
        else _positive(rated.flow_m3_h, 0.0)
    )
    leaving = wetbulb_temp_c + _clamp(approach_c, 1.0, 25.0)
    rise = heat_reject_kw / (WATER_KW_PER_M3H_K * flow_m3_h) if flow_m3_h > 0.0 else 0.0

    return CoolingTowerState(
        fan_speed_ratio=ratio,
        approach_c=approach_c,
        entering_water_temp_c=leaving + _clamp(rise, 0.0, 20.0),
        leaving_water_temp_c=leaving,
        flow_m3_h=flow_m3_h,
        fan_power_kw=_positive(rated.fan_power_kw, rated.power_kw or 0.0) * ratio**3,
    )


def counterflow_effectiveness(ntu: float, capacity_ratio: float) -> float:
    """逆流换热器效能 ε(NTU, Cr)

        Cr < 1:  ε = (1 - exp(-NTU·(1-Cr))) / (1 - Cr·exp(-NTU·(1-Cr)))
        Cr = 1:  ε = NTU / (1 + NTU)

    量纲：NTU 与 Cr 均为无量纲，ε 落在 [0, 1)。

    Cr = 1 时分母为零，必须走极限式；NTU = 0 时 ε = 0（无传热面即无换热）。
    """
    if not math.isfinite(ntu) or not math.isfinite(capacity_ratio):
        return 0.0
    if ntu <= 0.0:
        return 0.0
    ratio = _clamp(capacity_ratio, 0.0, 1.0)
    if ratio >= 1.0 - 1e-9:
        return ntu / (1.0 + ntu)
    exponent = math.exp(-ntu * (1.0 - ratio))
    return (1.0 - exponent) / (1.0 - ratio * exponent)


def _coil_capacity_flows(
    airflow_m3_h: float, water_flow_m3_h: float
) -> tuple[float, float]:
    """空气侧与水侧热容流率 C = ṁ·c_p，单位 kW/K

    C_air = V(m3/h)·ρ·c_p/3600，与 `AIR_KW_PER_M3H_K` 同一口径；
    C_w   = V(m3/h)·ρ_w·c_p,w/3600，与 `WATER_KW_PER_M3H_K` 同一口径。
    """
    return (
        AIR_KW_PER_M3H_K * airflow_m3_h,
        WATER_KW_PER_M3H_K * water_flow_m3_h,
    )


def ntu_from_counterflow_effectiveness(effectiveness: float, capacity_ratio: float) -> float:
    """逆流换热器 ε -> NTU 的解析反解

    由 ε = (1-x)/(1-Cr·x)（x = exp(-NTU(1-Cr))）解出：

        x  = (1-ε)/(1-ε·Cr)
        NTU = -ln(x)/(1-Cr),   Cr = 1 时 NTU = ε/(1-ε)

    量纲：ε 与 Cr 无量纲，NTU 无量纲。ε 必须落在 [0,1)，否则无解——
    这对应"要求的换热量超过 C_min·ΔT 热力学上限"，调用方须先判据。
    解析反解优于数值求根：无迭代、无收敛容差、无平台误差。
    """
    if not math.isfinite(effectiveness) or not math.isfinite(capacity_ratio):
        return 0.0
    if effectiveness <= 0.0:
        return 0.0
    effectiveness = _clamp(effectiveness, 0.0, 1.0 - 1e-12)
    ratio = _clamp(capacity_ratio, 0.0, 1.0)
    if ratio >= 1.0 - 1e-9:
        return effectiveness / (1.0 - effectiveness)
    x = (1.0 - effectiveness) / (1.0 - effectiveness * ratio)
    if x <= 0.0:
        return 0.0
    return -math.log(x) / (1.0 - ratio)


def _rated_coil_ua(
    rated: RatedSpec, capacity_kw: float, rated_airflow_m3_h: float, rated_water_flow: float
) -> tuple[float, bool]:
    """由目录额定点反标定盘管总传热系数 UA（kW/K）

    这是 ε-NTU 模型唯一需要标定的参数。目录只给额定容量，没有 UA，因此按
    **额定工况下 Q 严格等于目录容量** 反解：

        ε_rated = Q_rated / (C_min,rated · (T_air,rated - T_w,rated))
        NTU_rated = ε_rated -> NTU 的解析反解
        UA = NTU_rated · C_min,rated

    量纲：ε_rated 无量纲（必须 < 1），C_min 为 kW/K，UA 为 kW/K。

    返回 `(UA, limited)`：`limited=True` 表示目录容量超过 `C_min·ΔT` 这一
    单程盘管的热力学上限，ε>=1 无解，实际取上限容量。此时该型号在额定工况
    下**无法**复现目录容量，必须由调用方标记降级（约 25/64 个目录末端属于
    此类：全部 10 个新风机组、15 个 8 排 AHU）。

    风量或水量为零时无换热面，返回 (0, False)——此时 Q 恒为 0，与物理一致，
    不属于标定失败。
    """
    c_air = AIR_KW_PER_M3H_K * rated_airflow_m3_h
    c_water = WATER_KW_PER_M3H_K * rated_water_flow
    if capacity_kw <= 0.0 or c_air <= 0.0 or c_water <= 0.0:
        return 0.0, False

    c_min = min(c_air, c_water)
    cr = c_min / max(c_air, c_water)
    rated_delta = COIL_RATED_AIR_TEMP_C - COIL_RATED_WATER_TEMP_C
    if rated_delta <= 0.0:
        return 0.0, False

    required_effectiveness = capacity_kw / (c_min * rated_delta)
    limited = required_effectiveness >= 1.0
    required_effectiveness = min(required_effectiveness, 1.0 - 1e-12)
    return (
        ntu_from_counterflow_effectiveness(required_effectiveness, cr) * c_min,
        limited,
    )


# 盘管总传热系数中管内侧对流量依赖较弱（湍流强制对流 Nu ∝ Re^0.8），
# 风侧占比更大且由风机转速单独处理。这里只把水流量对 UA 的影响显式建模，
# 指数取 0.8 是 Dittus-Boelter 的经典值。
COIL_WATER_FLOW_UA_EXPONENT = 0.8


def _coil_ua_at_flow(ua_rated: float, water_flow_ratio: float) -> float:
    """水流量偏离额定时的盘管 UA

    UA 是一串热阻的串联：管内侧对流 + 管壁 + 翅片侧对流。水流量下降主要
    抬高管内侧热阻，故 UA 随流量减弱但弱于线性：

        UA/V̇_w < 1 时按 V̇_w^0.8 衰减；流量为零时 UA 为零（无换热面）。

    量纲：ua_rated 为 kW/K，water_flow_ratio 无量纲，返回 kW/K。

    没有这一项时，水量减半只改变 C_max 而 UA 不变，末端冷量几乎不随水量
    变化（实测半流量仍有 96% 能力），"调水量看末端能力下降"的教学演示
    就失效了。加入后水量下降同时抬高 NTU 分母与降低 UA，双重衰减。
    """
    if ua_rated <= 0.0:
        return 0.0
    if not math.isfinite(water_flow_ratio) or water_flow_ratio <= 0.0:
        return 0.0
    ratio = _clamp(water_flow_ratio, 0.0, 1.0)
    return ua_rated * ratio**COIL_WATER_FLOW_UA_EXPONENT


def terminal_state(
    rated: RatedSpec,
    valve_opening_ratio: float,
    room_temp_c: float,
    supply_water_temp_c: float,
    fan_speed_ratio: float = 1.0,
    enabled: bool = True,
    actual_water_flow_m3_h: float | None = None,
) -> TerminalState:
    """末端盘管：按 ε-NTU 逆流换热器计算换热量

    模型替换了原先的经验式 `capacity·sqrt(flow_ratio)·clamp(ΔT/12, 0, 1)`。
    旧式在 ΔT >= 12 K 时把温差项钳到 1.0，而冷冻水系统实际温差约 19 K
    （室温 26、供水 7），供水温因此完全不参与换热——改供水温设定值对房间
    温度没有影响，教学演示失效。ε-NTU 下供水温、水量、风量、室温都以物理
    方式进入同一组热容流率，不再有平台区。

        C_air = V_air·ρ·c_p/3600      [kW/K]
        C_w   = V_w·ρ_w·c_p,w/3600    [kW/K]
        C_min = min(C_air, C_w),  Cr = C_min/C_max
        ε     = ε_逆流(NTU, Cr),   NTU = UA/C_min
        Q     = ε·C_min·(T_air - T_w)          [kW]

    UA 由目录额定点反标定（见 `_rated_coil_ua`），因此额定工况下 Q 严格等于
    目录 `capacity_kw`，选型结论不变，只修正偏离额定点时的响应。

    风侧显热与全热的换算仍沿用 `SENSIBLE_HEAT_RATIO`：目录容量为全热能力，
    只有显热部分体现为送风温降。送风温的物理上限由 ε 本身给出（出风温不可能
    越过供水温），不再需要独立的经验常数。
    """
    opening = _clamp(valve_opening_ratio, 0.0, 1.0) if enabled else 0.0
    fan_ratio = _clamp(fan_speed_ratio, 0.0, 1.0) if enabled else 0.0
    room_temp_c = room_temp_c if math.isfinite(room_temp_c) else COIL_RATED_AIR_TEMP_C
    supply_water_temp_c = (
        supply_water_temp_c
        if math.isfinite(supply_water_temp_c)
        else COIL_RATED_WATER_TEMP_C
    )

    capacity_kw = _positive(rated.capacity_kw, 0.0)
    rated_airflow_m3_h = _positive(rated.airflow_m3_h, 0.0)
    airflow_m3_h = rated_airflow_m3_h * fan_ratio
    rated_water_flow = _positive(rated.flow_m3_h, 0.0)

    if actual_water_flow_m3_h is None:
        water_flow_m3_h = rated_water_flow * opening
    else:
        finite_flow = (
            actual_water_flow_m3_h if math.isfinite(actual_water_flow_m3_h) else 0.0
        )
        water_flow_m3_h = _clamp(finite_flow, 0.0, rated_water_flow) if enabled else 0.0

    ua_rated, calibration_limited = _rated_coil_ua(
        rated, capacity_kw, rated_airflow_m3_h, rated_water_flow
    )
    # 水流量偏离额定会抬高管内侧热阻，UA 随之下降。水量与 UA 同时衰减，
    # 末端能力因此对水量敏感（见 `_coil_ua_at_flow` 的说明）。
    water_flow_ratio = (
        water_flow_m3_h / rated_water_flow if rated_water_flow > 0.0 else 0.0
    )
    ua_kw_per_k = _coil_ua_at_flow(ua_rated, water_flow_ratio)

    c_air, c_water = _coil_capacity_flows(airflow_m3_h, water_flow_m3_h)
    cooling_delta = max(room_temp_c - supply_water_temp_c, 0.0)
    heating_delta = max(supply_water_temp_c - room_temp_c, 0.0)

    cooling_kw = 0.0
    heating_kw = 0.0
    c_min = min(c_air, c_water)
    if c_min > 0.0 and ua_kw_per_k > 0.0:
        cr = c_min / max(c_air, c_water)
        effectiveness = counterflow_effectiveness(ua_kw_per_k / c_min, cr)
        # 制冷与制热共用同一台盘管，按当前换热方向取温差。两者互斥：
        # 供水温低于室温即制冷，高于室温即制热，不会同时为正。
        cooling_kw = effectiveness * c_min * cooling_delta
        heating_kw = effectiveness * c_min * heating_delta

    # 水侧能量守恒硬约束：Q 不可能超过把水量加热/冷却到 dT 上限所需的能量。
    water_side_limit_kw = WATER_KW_PER_M3H_K * water_flow_m3_h * MAX_HYDRONIC_DELTA_T_C
    cooling_kw = min(cooling_kw, water_side_limit_kw)
    heating_kw = min(heating_kw, water_side_limit_kw)

    supply_air_temp_c = room_temp_c
    if airflow_m3_h > 0.0 and cooling_kw > 0.0:
        sensible_kw = cooling_kw * SENSIBLE_HEAT_RATIO
        drop = sensible_kw / (AIR_KW_PER_M3H_K * airflow_m3_h)
        # 显热温降不得超过可用温差——盘管不可能把空气冷却到供水温以下。
        supply_air_temp_c = room_temp_c - _clamp(drop, 0.0, cooling_delta)
    elif airflow_m3_h > 0.0 and heating_kw > 0.0:
        sensible_kw = heating_kw * SENSIBLE_HEAT_RATIO
        rise = sensible_kw / (AIR_KW_PER_M3H_K * airflow_m3_h)
        supply_air_temp_c = room_temp_c + _clamp(rise, 0.0, heating_delta)

    return TerminalState(
        valve_opening_ratio=opening,
        cooling_kw=cooling_kw,
        heating_kw=heating_kw,
        airflow_m3_h=airflow_m3_h,
        water_flow_m3_h=water_flow_m3_h,
        supply_air_temp_c=supply_air_temp_c,
        power_kw=_positive(rated.fan_power_kw, 0.0) * fan_ratio**3,
        coil_calibration_limited=calibration_limited,
    )


def coil_leaving_state(
    entering_temp_c: float,
    entering_ratio: float,
    supply_airflow_m3_h: float,
    total_cooling_kw: float,
) -> tuple[float, float]:
    """盘管对进风冷却后的送风状态（干球温度, 含湿量）

    这是"盘管处理混合风"的核心：进风可以是回风、新风或两者混合，出风状态
    由能量守恒与饱和线共同决定，因此**新风的湿负荷必须穿过盘管**，无法绕过。

    判定分两支：
    - **干盘管**（等湿降温）：按定含湿量沿焓线降温，不除湿；
    - **湿盘管**（降温除湿）：出口落在饱和线上，含湿量取该温度的饱和值。

    先用干盘管假设求出口温度，若该温度下的饱和含湿量低于进风含湿量，说明
    空气已过饱和、必然结露，改走湿盘管分支按饱和线反解。

    风量为零或无冷量时原样返回进风状态，避免除零，也保证不凭空造出送风温差。
    """
    if (
        supply_airflow_m3_h <= 0.0
        or total_cooling_kw <= 0.0
        or not math.isfinite(entering_temp_c)
        or not math.isfinite(entering_ratio)
    ):
        return entering_temp_c, entering_ratio

    air_mass_kg_s = supply_airflow_m3_h * AIR_DENSITY_KG_M3 / 3600.0
    if air_mass_kg_s <= 0.0:
        return entering_temp_c, entering_ratio

    enthalpy_in = enthalpy_kj_kg(entering_temp_c, entering_ratio)
    enthalpy_out = enthalpy_in - total_cooling_kw / air_mass_kg_s

    # 干盘管假设：含湿量不变，按焓反解出口温度。
    # 极小风量配极大冷量会解出远离工况的假想温度，先钳到合理下界，
    # 避免后续按饱和线判据时落入数值无意义区。
    dry_temperature = max(
        (enthalpy_out - VAPORIZATION_HEAT_KJ_KG * entering_ratio)
        / (DRY_AIR_CP_KJ_KG_K + VAPOR_CP_KJ_KG_K * entering_ratio),
        MIN_COIL_LEAVING_TEMP_C,
    )

    # 出口处于过饱和（饱和含湿量低于进风含湿量）则实际会结露
    if entering_ratio > saturation_humidity_ratio(dry_temperature):
        saturated_temperature = max(
            temperature_from_saturated_enthalpy(enthalpy_out),
            MIN_COIL_LEAVING_TEMP_C,
        )
        return saturated_temperature, saturation_humidity_ratio(saturated_temperature)

    return dry_temperature, entering_ratio


def valve_state(opening_ratio: float, enabled: bool = True) -> ValveState:
    """阀门：开度与阻力倍率

    开度趋零时阻力发散，故取下限截断，保证水力计算不除零。
    """
    opening = _clamp(opening_ratio, 0.0, 1.0) if enabled else 0.0
    effective = max(opening, 0.02)
    return ValveState(opening_ratio=opening, resistance_ratio=1.0 / effective**2)


def water_delta_t(load_kw: float, flow_m3_h: float) -> float:
    """水环路温差：ΔT = Q / (c · m)

    流量降低时温差拉大，这是"调水泵转速看供回水温变化"演示的物理基础。
    """
    if flow_m3_h <= 0.0 or load_kw <= 0.0:
        return 0.0
    return load_kw / (WATER_KW_PER_M3H_K * flow_m3_h)


def _clamp(value: float, low: float, high: float) -> float:
    if math.isnan(value):
        return low
    return max(low, min(high, value))


def _positive(value: float | None, default: float) -> float:
    if value is None or value <= 0.0:
        return default
    return float(value)


def is_pump(role: DeviceRole) -> bool:
    return role in {
        DeviceRole.PUMP,
        DeviceRole.PUMP_CHILLED_WATER,
        DeviceRole.PUMP_COOLING_WATER,
    }


def is_terminal(role: DeviceRole) -> bool:
    return role in {DeviceRole.FCU, DeviceRole.AHU, DeviceRole.FRESH_AIR_UNIT}


def is_valve(role: DeviceRole) -> bool:
    return role in {
        DeviceRole.WATER_VALVE,
        DeviceRole.AIR_VALVE,
        DeviceRole.FIRE_DAMPER,
    }
