"""Hardy Cross 流量求解器 - 包初始化"""

from app.hardy_cross.models import (
    NetworkModel,
    NetworkNode,
    PipeEdge,
    Loop,
    PumpCurve,
    SolverResult,
    SolverConfig,
)
from app.hardy_cross.loop_detector import detect_loops
from app.hardy_cross.solver import solve_hardy_cross
from app.hardy_cross.resistance import (
    calculate_resistance_coefficient,
    calculate_pressure_drop,
)

__all__ = [
    # 数据模型
    "NetworkModel",
    "NetworkNode",
    "PipeEdge",
    "Loop",
    "PumpCurve",
    "SolverResult",
    "SolverConfig",
    # 核心功能
    "detect_loops",
    "solve_hardy_cross",
    "calculate_resistance_coefficient",
    "calculate_pressure_drop",
]
