"""可调参数定义

固定参数由选型锁定（见 `rated.py`），本模块描述运行期学生能改什么、范围多大。
范围随型号给出——例如定速泵只能启停、变频泵才有连续调速——前端据此渲染控件，
不必各自硬编码上下限。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

from app.simcore.devices import is_terminal, is_valve
from app.simcore.rated import RatedSpec
from app.unity_catalog import DeviceRole


ControlKind = Literal["number", "switch"]


@dataclass(frozen=True, slots=True)
class ControlSpec:
    """单个可调参数的取值约束"""

    name: str
    label: str
    unit: str
    minimum: float
    maximum: float
    step: float
    default: float
    kind: ControlKind = "number"

    def clamp(self, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError(f"Control {self.name} must be finite")
        bounded = max(self.minimum, min(self.maximum, value))
        steps = math.floor((bounded - self.minimum) / self.step + 0.5)
        snapped = self.minimum + steps * self.step
        return max(self.minimum, min(self.maximum, snapped))


_ENABLED = ControlSpec(
    name="enabled",
    label="启停",
    unit="",
    minimum=0.0,
    maximum=1.0,
    step=1.0,
    default=1.0,
    kind="switch",
)

_SUPPLY_TEMP = ControlSpec(
    name="supply_temp_c",
    label="出水温度设定",
    unit="C",
    minimum=4.0,
    maximum=15.0,
    step=0.5,
    default=7.0,
)

_HOT_WATER_SUPPLY_TEMP = ControlSpec(
    name="supply_temp_c",
    label="热水出水温度设定",
    unit="C",
    minimum=30.0,
    maximum=55.0,
    step=0.5,
    default=45.0,
)

_VALVE_OPENING = ControlSpec(
    name="valve_opening_pct",
    label="水阀开度",
    unit="%",
    minimum=0.0,
    maximum=100.0,
    step=1.0,
    default=100.0,
)

_OPENING = ControlSpec(
    name="opening_pct",
    label="开度",
    unit="%",
    minimum=0.0,
    maximum=100.0,
    step=1.0,
    default=100.0,
)


def _speed_control(name: str, label: str, variable_speed: bool) -> ControlSpec:
    """转速控制

    定速设备只能全开或停机，范围退化为两档；变频设备才给连续调节。
    这样"这台泵是不是变频"这个固定参数会直接体现在可调范围上。
    """
    if variable_speed:
        return ControlSpec(
            name=name,
            label=label,
            unit="%",
            minimum=0.0,
            maximum=100.0,
            step=1.0,
            default=100.0,
        )
    return ControlSpec(
        name=name,
        label=label,
        unit="%",
        minimum=0.0,
        maximum=100.0,
        step=100.0,
        default=100.0,
        kind="switch",
    )


def _active_count(quantity: int) -> ControlSpec:
    return ControlSpec(
        name="active_count",
        label="运行台数",
        unit="台",
        minimum=0.0,
        maximum=float(quantity),
        step=1.0,
        default=float(quantity),
    )


def controls_for(rated: RatedSpec, quantity: int = 1) -> tuple[ControlSpec, ...]:
    """列出该设备的全部可调参数"""
    role = rated.role
    bank = (_active_count(quantity),) if quantity > 1 else ()

    if role is DeviceRole.CHILLER:
        return (_ENABLED, *bank, _SUPPLY_TEMP)

    if role is DeviceRole.HEAT_SOURCE:
        return (_ENABLED, *bank, _HOT_WATER_SUPPLY_TEMP)

    if role in {
        DeviceRole.PUMP,
        DeviceRole.PUMP_CHILLED_WATER,
        DeviceRole.PUMP_COOLING_WATER,
    }:
        return (
            _ENABLED,
            *bank,
            _speed_control("speed_pct", "转速", rated.variable_speed),
        )

    if role is DeviceRole.COOLING_TOWER:
        # 冷却塔风机在本模型中一律按可调速处理，逼近度随转速连续变化
        return (_ENABLED, *bank, _speed_control("fan_speed_pct", "风机转速", True))

    if is_terminal(role):
        return (
            _ENABLED,
            *bank,
            _VALVE_OPENING,
            _speed_control("fan_speed_pct", "风机转速", True),
        )

    if is_valve(role):
        return (_ENABLED, *bank, _OPENING)

    return (_ENABLED, *bank)


def default_controls(rated: RatedSpec, quantity: int = 1) -> dict[str, float]:
    """该设备的控制初值"""
    return {spec.name: spec.default for spec in controls_for(rated, quantity)}


def clamp_controls(
    rated: RatedSpec, values: dict[str, float], quantity: int = 1
) -> dict[str, float]:
    """按可调范围钳制，并丢弃该设备不认识的参数名"""
    specs = {spec.name: spec for spec in controls_for(rated, quantity)}
    return {
        name: specs[name].clamp(float(value))
        for name, value in values.items()
        if name in specs
    }


def unknown_control_names(
    rated: RatedSpec, values: dict[str, float], quantity: int = 1
) -> tuple[str, ...]:
    """列出该设备不支持的参数名，供接口回报而不是静默忽略"""
    specs = {spec.name for spec in controls_for(rated, quantity)}
    return tuple(name for name in values if name not in specs)
