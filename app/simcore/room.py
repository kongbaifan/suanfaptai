"""房间热湿模型

室温按热容积分推进。制冷量不使用与设备无关的比例系数，而是由末端设备的
实际换热量传入——换末端型号，室温才会跟着变。

湿度同理按含湿量积分，见 `step_room_humidity`。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from app.simcore.psychrometrics import (
    LATENT_HEAT_KJ_KG,
    MIN_HUMIDITY_RATIO,
    humidity_ratio_from_rh,
    rh_from_humidity_ratio,
)


# 人员显热得热
PERSON_SENSIBLE_KW = 0.075

# 人员潜热散湿，与显热同为常规办公取值（合计约 130 W/人）
PERSON_LATENT_KW = 0.055

# 干空气密度
AIR_DENSITY_KG_M3 = 1.2

# 房间湿度物理边界
MIN_ROOM_RH_PCT = 10.0
MAX_ROOM_RH_PCT = 95.0

# 室温物理边界，防止参数异常时读数发散
MIN_ROOM_TEMP_C = 10.0
MAX_ROOM_TEMP_C = 40.0


@dataclass(frozen=True, slots=True)
class RoomThermalConfig:
    """房间热特性

    默认值取自 legacy `RuntimeConfig`，保证动力学基准同源。
    """

    thermal_capacity_kj_k: float = 1800.0
    envelope_conductance_kw_k: float = 0.18
    occupancy_people: int = 8
    lighting_w: float = 800.0
    equipment_w: float = 1200.0
    volume_m3: float = 150.0

    def __post_init__(self) -> None:
        if self.thermal_capacity_kj_k <= 0.0:
            raise ValueError("room thermal capacity must be positive")
        if self.volume_m3 <= 0.0:
            raise ValueError("room volume must be positive")

    @property
    def internal_gain_kw(self) -> float:
        """室内得热：人员显热 + 照明 + 设备"""
        return (
            self.occupancy_people * PERSON_SENSIBLE_KW
            + self.lighting_w / 1000.0
            + self.equipment_w / 1000.0
        )

    @property
    def air_mass_kg(self) -> float:
        """房间干空气质量，湿平衡的分母"""
        return self.volume_m3 * AIR_DENSITY_KG_M3

    @property
    def latent_gain_kw(self) -> float:
        """室内散湿折合的潜热，目前只计人员"""
        return self.occupancy_people * PERSON_LATENT_KW


@dataclass(frozen=True, slots=True)
class RoomBalance:
    """单步热平衡明细，便于教学时逐项解释温度为何这样变"""

    temperature_c: float
    envelope_kw: float
    internal_gain_kw: float
    cooling_kw: float
    heating_kw: float
    net_kw: float


@dataclass(frozen=True, slots=True)
class RoomHumidityBalance:
    """单步湿平衡明细

    与 `RoomBalance` 对称，逐项给出湿度为何这样变。潜热量单位统一为 kW，
    含湿量为 kg/kg。
    """

    relative_humidity_pct: float
    humidity_ratio: float
    infiltration_latent_kw: float
    internal_latent_kw: float
    dehumidification_kw: float
    net_latent_kw: float


def step_room_humidity(
    current_rh_pct: float,
    current_temp_c: float,
    next_temp_c: float,
    outdoor_temp_c: float,
    outdoor_rh_pct: float,
    latent_cooling_kw: float,
    config: RoomThermalConfig,
    step_seconds: float,
    *,
    fresh_air_m3_h: float = 0.0,
    fresh_air_latent_capacity_kw: float = 0.0,
) -> RoomHumidityBalance:
    """推进一个时间步的房间湿度

    以含湿量为状态量做显式欧拉积分：
    `Δw = (新风带入 + 室内散湿 - 盘管除湿) · Δt / (空气质量 · 汽化潜热)`。

    三项都先折成潜热功率（kW）再统一换算，与显热侧的写法保持一致。
    新风项按室内外含湿量差计算，室外更湿则带入水分，反之带走。

    `fresh_air_latent_capacity_kw` 是新风机组的潜热能力，先就地抵扣新风湿负荷
    再把余量交给房间——新风机组的职责就是处理新风，若把它的潜热能力与内区盘管
    一样计入房间除湿，等于让同一份能力既处理新风又除房间的湿，会重复计账。

    新的相对湿度用 `next_temp_c` 反算——同一含湿量下降温会抬高相对湿度，
    这是制冷时室内偏湿的直接原因，教学上必须体现。
    """
    air_mass_kg = config.air_mass_kg
    current_ratio = humidity_ratio_from_rh(current_temp_c, current_rh_pct)
    outdoor_ratio = humidity_ratio_from_rh(outdoor_temp_c, outdoor_rh_pct)

    # 新风换气折合的潜热功率
    fresh_air_kg_s = (
        max(fresh_air_m3_h, 0.0) * AIR_DENSITY_KG_M3 / 3600.0
        if math.isfinite(fresh_air_m3_h)
        else 0.0
    )
    raw_fresh_air_latent_kw = (
        fresh_air_kg_s * (outdoor_ratio - current_ratio) * LATENT_HEAT_KJ_KG
    )
    # 新风机组只在新风加湿房间时起作用，且抵扣不能反向变成加湿
    offset_kw = (
        max(fresh_air_latent_capacity_kw, 0.0)
        if math.isfinite(fresh_air_latent_capacity_kw)
        else 0.0
    )
    treated_fresh_air_latent_kw = (
        max(raw_fresh_air_latent_kw - offset_kw, 0.0)
        if raw_fresh_air_latent_kw > 0.0
        else raw_fresh_air_latent_kw
    )
    infiltration_latent_kw = treated_fresh_air_latent_kw
    internal_latent_kw = config.latent_gain_kw
    # 非有限的冷量不能污染分项读数：RH 有钳制兜底，但分项会原样带出去
    dehumidification_kw = (
        max(latent_cooling_kw, 0.0) if math.isfinite(latent_cooling_kw) else 0.0
    )
    net_latent_kw = (
        infiltration_latent_kw + internal_latent_kw - dehumidification_kw
    )

    next_ratio = current_ratio + net_latent_kw * step_seconds / (
        air_mass_kg * LATENT_HEAT_KJ_KG
    )
    next_ratio = max(next_ratio, MIN_HUMIDITY_RATIO)
    next_rh = _clamp(
        rh_from_humidity_ratio(next_temp_c, next_ratio),
        MIN_ROOM_RH_PCT,
        MAX_ROOM_RH_PCT,
    )
    return RoomHumidityBalance(
        relative_humidity_pct=next_rh,
        # 湿度被边界截断时同步回算含湿量，状态量与读数不能互相矛盾
        humidity_ratio=humidity_ratio_from_rh(next_temp_c, next_rh),
        infiltration_latent_kw=infiltration_latent_kw,
        internal_latent_kw=internal_latent_kw,
        dehumidification_kw=dehumidification_kw,
        net_latent_kw=net_latent_kw,
    )


def step_room_temperature(
    current_temp_c: float,
    outdoor_temp_c: float,
    cooling_kw: float,
    config: RoomThermalConfig,
    step_seconds: float,
    *,
    heating_kw: float = 0.0,
) -> RoomBalance:
    """推进一个时间步

    显式欧拉积分：`ΔT = (围护得热 + 室内得热 - 制冷量) · Δt / 热容`。
    冷热量均为末端设备实际提供的正值，净热流明确使用 `+ heating - cooling`。
    """
    envelope_kw = config.envelope_conductance_kw_k * (outdoor_temp_c - current_temp_c)
    internal_kw = config.internal_gain_kw
    net_kw = envelope_kw + internal_kw + heating_kw - cooling_kw

    next_temp = current_temp_c + net_kw * step_seconds / config.thermal_capacity_kj_k
    return RoomBalance(
        temperature_c=_clamp(next_temp, MIN_ROOM_TEMP_C, MAX_ROOM_TEMP_C),
        envelope_kw=envelope_kw,
        internal_gain_kw=internal_kw,
        cooling_kw=cooling_kw,
        heating_kw=heating_kw,
        net_kw=net_kw,
    )


def equilibrium_temperature(
    outdoor_temp_c: float, cooling_kw: float, config: RoomThermalConfig
) -> float:
    """求给定制冷量下的稳态室温

    令净热流为零解出，供 `steady` 做初值估计，避免从任意初温积分过多步。
    """
    conductance = config.envelope_conductance_kw_k
    if conductance <= 0.0:
        return outdoor_temp_c
    surplus_kw = config.internal_gain_kw - cooling_kw
    return _clamp(
        outdoor_temp_c + surplus_kw / conductance, MIN_ROOM_TEMP_C, MAX_ROOM_TEMP_C
    )


def _clamp(value: float, low: float, high: float) -> float:
    if math.isnan(value):
        return low
    return max(low, min(high, value))
