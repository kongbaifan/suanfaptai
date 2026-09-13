"""Hardy Cross 流量求解器 - 数据结构

基于物理准确的管网压降平衡原理求解流量分配。
"""

from dataclasses import dataclass, field


@dataclass
class PipeEdge:
    """管道边"""
    id: str                           # 管道或连接ID
    from_node: str                    # 起始节点（设备端口）
    to_node: str                      # 终止节点（设备端口）
    diameter_mm: float                # 管径（mm）
    length_m: float                   # 长度（m）
    roughness_mm: float = 0.05        # 粗糙度（mm，钢管默认0.05）
    flow_m3_h: float = 0.0            # 流量（m³/h，待求解）
    resistance_coeff: float = 0.0     # 阻力系数 K（计算缓存）


@dataclass
class NetworkNode:
    """管网节点（设备端口）"""
    id: str                           # 节点ID（device_id:port_type）
    device_id: str                    # 设备ID
    port_type: str                    # 端口类型（supply/return/inlet/outlet）
    external_flow_m3_h: float = 0.0   # 外部注入流量（源>0，汇<0）
    pressure_pa: float | None = None  # 节点压力（Pa，待求解）


@dataclass
class Loop:
    """基本回路（用于 Hardy Cross 迭代）"""
    id: str                           # 回路ID
    edges: list[str]                  # 边ID列表（有序，沿回路方向）
    directions: list[int]             # 边在回路中的方向（1=正向，-1=反向）
    correction_m3_h: float = 0.0      # 流量修正量（m³/h）


@dataclass
class PumpCurve:
    """泵扬程曲线（二次拟合：H = a - b×Q²）"""
    device_id: str                    # 泵设备ID
    a: float                          # 截距（零流量扬程，m）
    b: float                          # 二次系数（m/(m³/h)²）
    rated_flow_m3_h: float            # 额定流量
    rated_head_m: float               # 额定扬程

    def head_at_flow(self, flow_m3_h: float) -> float:
        """计算给定流量下的扬程（m）"""
        return self.a - self.b * flow_m3_h**2

    def pressure_rise_pa(self, flow_m3_h: float, rho: float = 1000) -> float:
        """计算泵提供的压升（Pa）"""
        head_m = self.head_at_flow(flow_m3_h)
        return rho * 9.81 * head_m


@dataclass
class NetworkModel:
    """管网模型（包含节点、边、回路、泵）"""
    nodes: dict[str, NetworkNode]     # 节点字典
    edges: dict[str, PipeEdge]        # 边字典
    loops: list[Loop]                 # 回路列表
    pumps: dict[str, PumpCurve] = field(default_factory=dict)  # 泵曲线字典
    medium: str = "unknown"           # 介质类型

    def get_edges_from(self, node_id: str) -> list[PipeEdge]:
        """获取从某节点出发的所有边"""
        return [e for e in self.edges.values() if e.from_node == node_id]

    def get_edges_to(self, node_id: str) -> list[PipeEdge]:
        """获取到达某节点的所有边"""
        return [e for e in self.edges.values() if e.to_node == node_id]

    def get_connected_edges(self, node_id: str) -> list[PipeEdge]:
        """获取与某节点相连的所有边"""
        return self.get_edges_from(node_id) + self.get_edges_to(node_id)


@dataclass
class SolverResult:
    """求解器结果"""
    converged: bool                   # 是否收敛
    iterations: int                   # 迭代次数
    max_residual_m3_h: float          # 最大残差（m³/h）
    edge_flows: dict[str, float]      # 边流量字典 {edge_id: flow_m3_h}
    node_pressures: dict[str, float] | None = None  # 节点压力字典（可选）
    pump_operating_points: dict[str, tuple[float, float]] | None = None  # 泵工作点（flow, head）


@dataclass
class SolverConfig:
    """求解器配置"""
    max_iterations: int = 50          # 最大迭代次数
    tolerance_m3_h: float = 0.01      # 收敛容差（m³/h）
    flow_exponent: float = 1.852      # 流量指数（Hazen-Williams）
    hazen_williams_C: float = 120.0   # 粗糙系数（钢管）
    water_density: float = 1000.0     # 水密度（kg/m³）
    initial_flow_m3_h: float = 1.0    # 初始流量（m³/h）
