"""流量分配与守恒验证

实现：
- 需求分配（基于负荷和末端需求）
- 路径枚举（源到负荷的所有路径）
- 流量守恒残差计算（每个内部节点）
"""

from collections import defaultdict, deque
from dataclasses import dataclass
from .port_graph import PortGraph, Port, Edge, Medium


@dataclass
class FlowAllocation:
    """流量分配结果"""
    port_flow: dict[tuple[str, int], float]  # (device_id, port_index) -> flow (m3/h or kg/s)
    edge_flow: dict[int, float]  # edge_index -> flow
    conservation_residuals: dict[str, float]  # device_id -> residual (应该接近0)
    total_demand: float  # 总需求
    actual_flow_m3_h: float | None = None
    unmet_flow_m3_h: float = 0.0
    unmet_load_kw: float | None = None
    limiting_device_ids: tuple[str, ...] = ()


@dataclass
class NetworkPath:
    """网络路径（源到负荷）"""
    path_id: str
    edges: list[Edge]
    total_length_m: float
    total_resistance: float  # 总阻力


def allocate_flow(
    graph: PortGraph,
    medium: Medium,
    total_demand_m3_h: float,
    terminal_demands: dict[str, float] | None = None,
) -> FlowAllocation:
    """流量分配

    Args:
        graph: 端口图
        medium: 介质类型
        total_demand_m3_h: 总需求流量（m³/h）
        terminal_demands: 各末端需求 {device_id: flow_m3_h}

    Returns:
        FlowAllocation 包含端口流量、边流量和守恒残差
    """
    terminal_demands = terminal_demands or {}
    subgraph = graph.medium_subgraphs.get(medium)

    if not subgraph or not subgraph.edges:
        return FlowAllocation(
            port_flow={},
            edge_flow={},
            conservation_residuals={},
            total_demand=total_demand_m3_h,
        )

    # 1. 初始化：均分流量给所有边
    num_edges = len(subgraph.edges)
    avg_flow = total_demand_m3_h / max(num_edges, 1)

    edge_flow: dict[int, float] = {}
    port_flow: dict[tuple[str, int], float] = defaultdict(float)

    for idx, edge in enumerate(subgraph.edges):
        edge_flow[idx] = avg_flow

        # 累加端口流量
        from_key = (edge.from_port.device_id, edge.from_port.port_index)
        to_key = (edge.to_port.device_id, edge.to_port.port_index)
        port_flow[from_key] += avg_flow
        port_flow[to_key] += avg_flow

    # 2. 计算守恒残差（简化：每个设备的进出流量差）
    conservation_residuals = _calculate_conservation_residuals(subgraph, edge_flow)

    return FlowAllocation(
        port_flow=dict(port_flow),
        edge_flow=edge_flow,
        conservation_residuals=conservation_residuals,
        total_demand=total_demand_m3_h,
    )


def _calculate_conservation_residuals(subgraph: PortGraph, edge_flow: dict[int, float]) -> dict[str, float]:
    """计算守恒残差

    对每个设备，计算：residual = |sum(inflow) - sum(outflow)|
    理想情况下，residual 应接近 0
    """
    device_inflow: dict[str, float] = defaultdict(float)
    device_outflow: dict[str, float] = defaultdict(float)

    for idx, edge in enumerate(subgraph.edges):
        flow = edge_flow.get(idx, 0.0)

        # from_port 出流，to_port 入流
        device_outflow[edge.from_port.device_id] += flow
        device_inflow[edge.to_port.device_id] += flow

    # 计算残差
    all_devices = set(device_inflow.keys()) | set(device_outflow.keys())
    residuals: dict[str, float] = {}

    for device_id in all_devices:
        inflow = device_inflow.get(device_id, 0.0)
        outflow = device_outflow.get(device_id, 0.0)
        residuals[device_id] = abs(inflow - outflow)

    return residuals


def enumerate_paths(
    graph: PortGraph,
    medium: Medium,
    source_device_ids: list[str],
    sink_device_ids: list[str],
    max_paths: int = 10,
) -> list[NetworkPath]:
    """枚举网络路径（从源到负荷）

    Args:
        graph: 端口图
        medium: 介质类型
        source_device_ids: 源设备列表（如冷机、泵）
        sink_device_ids: 负荷设备列表（如 FCU、AHU）
        max_paths: 最大路径数

    Returns:
        NetworkPath 列表
    """
    subgraph = graph.medium_subgraphs.get(medium)
    if not subgraph:
        return []

    paths: list[NetworkPath] = []

    # 简化实现：从每个源到每个负荷查找一条最短路径
    for source_id in source_device_ids:
        for sink_id in sink_device_ids:
            path = _find_shortest_path(subgraph, source_id, sink_id)
            if path:
                paths.append(path)

            if len(paths) >= max_paths:
                return paths

    return paths


def _find_shortest_path(subgraph: PortGraph, source_id: str, sink_id: str) -> NetworkPath | None:
    """BFS 查找最短路径（边数最少）"""
    # 获取源和目标的所有端口
    source_ports = subgraph.device_ports.get(source_id, [])
    sink_ports = subgraph.device_ports.get(sink_id, [])

    if not source_ports or not sink_ports:
        return None

    # BFS 从源的任意端口开始
    queue: deque[tuple[Port, list[Edge]]] = deque(
        [(source_ports[0], [])]
    )  # (current_port, edges_so_far)
    visited: set[tuple[str, int]] = set()
    visited.add((source_ports[0].device_id, source_ports[0].port_index))

    while queue:
        current_port, edges_so_far = queue.popleft()
        current_key = (current_port.device_id, current_port.port_index)

        # 检查是否到达目标
        if current_port.device_id == sink_id:
            total_length = sum(e.length_m for e in edges_so_far)
            return NetworkPath(
                path_id=f"{source_id}_to_{sink_id}",
                edges=edges_so_far,
                total_length_m=total_length,
                total_resistance=0.0,  # 暂不计算
            )

        # 扩展邻居
        for edge in subgraph.adjacency.get(current_key, []):
            next_port = edge.to_port if edge.from_port == current_port else edge.from_port
            next_key = (next_port.device_id, next_port.port_index)

            if next_key not in visited:
                visited.add(next_key)
                queue.append((next_port, edges_so_far + [edge]))

    return None


def check_conservation_tolerance(residuals: dict[str, float], tolerance: float = 0.01) -> list[str]:
    """检查守恒残差是否在容差内

    Args:
        residuals: 设备守恒残差 {device_id: residual}
        tolerance: 容差（默认 0.01 m³/h）

    Returns:
        违反守恒的设备 ID 列表
    """
    violations = [device_id for device_id, residual in residuals.items() if residual > tolerance]
    return violations


def calculate_manifold_residuals(
    graph: PortGraph,
    medium: Medium,
    allocation: FlowAllocation,
) -> dict[str, float]:
    """Return |main flow - branch flow sum| for each manifold."""
    bindings = [
        binding
        for binding in graph.manifold_bindings
        if binding.medium == medium
    ]
    manifold_ids = sorted({binding.manifold_id for binding in bindings})
    residuals: dict[str, float] = {}

    for manifold_id in manifold_ids:
        branch_ports = {
            binding.port_index
            for binding in bindings
            if binding.manifold_id == manifold_id and binding.kind == "branch"
        }
        main_flow = allocation.port_flow.get((manifold_id, 0), 0.0)
        branch_flow = sum(
            allocation.port_flow.get((manifold_id, port_index), 0.0)
            for port_index in branch_ports
        )
        residuals[manifold_id] = abs(main_flow - branch_flow)

    return residuals
