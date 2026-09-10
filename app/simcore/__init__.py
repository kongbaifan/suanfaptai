"""统一仿真核心

`simcore` 是后端唯一的仿真实现。REST 稳态评估与 WS 实时推送都由这里驱动，
Vue 工作台消费同一份状态。

稳态不是独立的第二套物理——它是动态核跑到不动点的结果，见 `steady.py`。
"""

from app.simcore.controls import (
    ControlSpec,
    clamp_controls,
    controls_for,
    default_controls,
    unknown_control_names,
)
from app.simcore.rated import (
    ParameterOrigin,
    RatedSpec,
    resolve_rated_spec,
    resolve_rated_spec_by_type,
)
from app.simcore.room import RoomThermalConfig
from app.simcore.session import (
    DeviceNode,
    DeviceReading,
    PlantLayout,
    PlantSnapshot,
    RoomInitialState,
    RoomNode,
    RoomReading,
    SimulationConfig,
    SimulationMode,
    SimulationSession,
)
from app.simcore.steady import SteadyResult, solve_steady
from app.simcore.weather import Ambient, ambient_at, wet_bulb_c

__all__ = [
    "Ambient",
    "ControlSpec",
    "DeviceNode",
    "DeviceReading",
    "ParameterOrigin",
    "PlantLayout",
    "PlantSnapshot",
    "RatedSpec",
    "RoomInitialState",
    "RoomNode",
    "RoomReading",
    "RoomThermalConfig",
    "SimulationConfig",
    "SimulationMode",
    "SimulationSession",
    "SteadyResult",
    "ambient_at",
    "clamp_controls",
    "controls_for",
    "default_controls",
    "resolve_rated_spec",
    "resolve_rated_spec_by_type",
    "solve_steady",
    "unknown_control_names",
    "wet_bulb_c",
]
