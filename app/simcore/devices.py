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
from app.simcore.rated import RatedSpec
from app.unity_catalog import DeviceRole


# 风侧：Q(kW) = V(m3/h) · ρ · c_p / 3600 · ΔT
AIR_KW_PER_M3H_K = 1.2 * 1.005 / 3600.0

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

# 目录标称容量为全热能力，只有显热部分体现为送风温降
SENSIBLE_HEAT_RATIO = 0.75
# 盘管效能上限：送风温最多逼近供水温到这个比例，不可能完全达到
MAX_COIL_EFFECTIVENESS = 0.85


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
    rated_cop = _positive(rated.cop, 4.5)
    curvature = _plr_curvature(rated)
    # 曲线在 PEAK_EFFICIENCY_PLR 处取峰值，并归一化为满负荷时 f(1.0) = 1.0
    peak = 1.0 + curvature * (1.0 - PEAK_EFFICIENCY_PLR) ** 2
    plr_factor = peak - curvature * (load_ratio - PEAK_EFFICIENCY_PLR) ** 2
    condenser_factor = 1.0 - CONDENSER_COP_PENALTY_PER_K * (
        condenser_temp_c - CONDENSER_REFERENCE_C
    )
    cop = rated_cop * _clamp(plr_factor, 0.25, 1.6) * _clamp(condenser_factor, 0.4, 1.3)
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
    entering = wetbulb_temp_c + _clamp(approach_c, 1.0, 25.0)
    rise = heat_reject_kw / (WATER_KW_PER_M3H_K * flow_m3_h) if flow_m3_h > 0.0 else 0.0

    return CoolingTowerState(
        fan_speed_ratio=ratio,
        approach_c=approach_c,
        entering_water_temp_c=entering,
        leaving_water_temp_c=entering + _clamp(rise, 0.0, 20.0),
        flow_m3_h=flow_m3_h,
        fan_power_kw=_positive(rated.fan_power_kw, rated.power_kw or 0.0) * ratio**3,
    )


def terminal_state(
    rated: RatedSpec,
    valve_opening_ratio: float,
    room_temp_c: float,
    supply_water_temp_c: float,
    fan_speed_ratio: float = 1.0,
    enabled: bool = True,
    actual_water_flow_m3_h: float | None = None,
) -> TerminalState:
    """末端：盘管换热受实际水量、风量与供水温共同限制。"""
    opening = _clamp(valve_opening_ratio, 0.0, 1.0) if enabled else 0.0
    fan_ratio = _clamp(fan_speed_ratio, 0.0, 1.0) if enabled else 0.0

    capacity_kw = _positive(rated.capacity_kw, 0.0)
    airflow_m3_h = _positive(rated.airflow_m3_h, 0.0) * fan_ratio
    rated_water_flow = _positive(rated.flow_m3_h, 0.0)
    if actual_water_flow_m3_h is None:
        water_flow_m3_h = rated_water_flow * opening
        water_factor = opening
    else:
        finite_flow = (
            actual_water_flow_m3_h if math.isfinite(actual_water_flow_m3_h) else 0.0
        )
        water_flow_m3_h = _clamp(finite_flow, 0.0, rated_water_flow) if enabled else 0.0
        flow_ratio = (
            water_flow_m3_h / rated_water_flow if rated_water_flow > 0.0 else 0.0
        )
        # A teaching coil retains part of its capacity as water flow falls;
        # the sublinear curve also makes Q/(c*V) delta-T widen at low flow.
        water_factor = math.sqrt(flow_ratio)

    # 供水温越接近室温，可用换热温差越小，冷热能力都按温差线性衰减。
    cooling_delta = max(room_temp_c - supply_water_temp_c, 0.0)
    heating_delta = max(supply_water_temp_c - room_temp_c, 0.0)
    cooling_kw = (
        capacity_kw * water_factor * fan_ratio * _clamp(cooling_delta / 12.0, 0.0, 1.0)
    )
    heating_kw = (
        capacity_kw * water_factor * fan_ratio * _clamp(heating_delta / 20.0, 0.0, 1.0)
    )
    water_side_limit_kw = (
        WATER_KW_PER_M3H_K * water_flow_m3_h * MAX_HYDRONIC_DELTA_T_C
    )
    cooling_kw = min(cooling_kw, water_side_limit_kw)
    heating_kw = min(heating_kw, water_side_limit_kw)

    supply_air_temp_c = room_temp_c
    if airflow_m3_h > 0.0 and cooling_kw > 0.0:
        # 目录容量为全热能力，只有显热部分体现为送风温降
        sensible_kw = cooling_kw * SENSIBLE_HEAT_RATIO
        drop = sensible_kw / (AIR_KW_PER_M3H_K * airflow_m3_h)
        # 盘管效能有限，送风温不可能压到供水温
        supply_air_temp_c = room_temp_c - _clamp(
            drop, 0.0, cooling_delta * MAX_COIL_EFFECTIVENESS
        )
    elif airflow_m3_h > 0.0 and heating_kw > 0.0:
        sensible_kw = heating_kw * SENSIBLE_HEAT_RATIO
        rise = sensible_kw / (AIR_KW_PER_M3H_K * airflow_m3_h)
        supply_air_temp_c = room_temp_c + _clamp(
            rise, 0.0, heating_delta * MAX_COIL_EFFECTIVENESS
        )

    return TerminalState(
        valve_opening_ratio=opening,
        cooling_kw=cooling_kw,
        heating_kw=heating_kw,
        airflow_m3_h=airflow_m3_h,
        water_flow_m3_h=water_flow_m3_h,
        supply_air_temp_c=supply_air_temp_c,
        power_kw=_positive(rated.fan_power_kw, 0.0) * fan_ratio**3,
    )


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
