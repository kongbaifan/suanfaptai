"""Hardy Cross 流量求解器 - 包初始化"""

from app.hardy_cross.loop_detector import detect_loops
from app.hardy_cross.models import (
    Loop,
    NetworkModel,
    NetworkNode,
    PipeEdge,
    PumpCurve,
    SolverConfig,
    SolverResult,
)
from app.hardy_cross.resistance import (
    calculate_pressure_drop,
    calculate_resistance_coefficient,
)
from app.hardy_cross.solver import solve_hardy_cross

# 按"数据模型 / 核心功能"分组列出，便于阅读；刻意不按字母序排列。
__all__ = [  # noqa: RUF022
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
