"""稳态管网与设备求解器

基于标准 DTO 和设备目录实现端口级拓扑、水力/风网和 HVAC 设备的确定性稳态求解。

Modules:
    port_graph: 端口级拓扑图构建
    topology_validator: 拓扑结构验证（断路、混接、供回接反）
    flow_solver: 流量分配与守恒验证
    hydraulic_solver: 水力网络求解
    air_solver: 风网求解
    equipment_models: 设备稳态模型（冷机、泵、塔、末端）
    solver: 主求解器入口
"""

from .solver import SteadySolverResult, solve_steady_state

__all__ = ["SteadySolverResult", "solve_steady_state"]
