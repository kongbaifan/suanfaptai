"""流量分配与守恒验证

实现：
- 需求分配（基于负荷和末端需求）
- 路径枚举（源到负荷的所有路径）
- 流量守恒残差计算（每个内部节点）
"""

from collections import defaultdict, deque
from dataclasses import dataclass

from .port_graph import Edge, Medium, Port, PortGraph


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
    # 求解器降级原因（非 None 表示这份流量来自回退方案，例如 Hardy-Cross 未收敛）。
    # 按「降级透明」约定，调用方必须把它作为 warning 显式上报，不能静默回退。
    degraded_reason: str | None = None


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

    给了 `terminal_demands` 就按需求沿生成森林路由（保证每个设备进出守恒）；
    没给就退回按边数均分。

    Args:
        graph: 端口图
        medium: 介质类型
        total_demand_m3_h: 总需求流量（m³/h）
        terminal_demands: 各末端需求 {device_id: flow_m3_h}
            - 正值表示源设备（注入流量）
            - 负值表示汇设备（消耗流量）

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

    if terminal_demands:
        edge_flow = _route_demand_on_spanning_forest(subgraph, terminal_demands)
    else:
        # 无需求信息时只能按边数均分。注意这不守恒（残差 = |入度-出度| × 均分流量），
        # 会报出 conservation_violation；仅用于调用方确实拿不到需求的场景。
        num_edges = len(subgraph.edges)
        avg_flow = total_demand_m3_h / max(num_edges, 1)
        edge_flow = {index: avg_flow for index in range(num_edges)}

    port_flow: dict[tuple[str, int], float] = defaultdict(float)
    for index, edge in enumerate(subgraph.edges):
        flow = edge_flow.get(index, 0.0)
        port_flow[(edge.from_port.device_id, edge.from_port.port_index)] += abs(flow)
        port_flow[(edge.to_port.device_id, edge.to_port.port_index)] += abs(flow)

    # 计算守恒残差（简化：每个设备的进出流量差）
    conservation_residuals = _calculate_conservation_residuals(
        subgraph, edge_flow, terminal_demands
    )

    return FlowAllocation(
        port_flow=dict(port_flow),
        edge_flow=edge_flow,
        conservation_residuals=conservation_residuals,
        total_demand=total_demand_m3_h,
    )


def _route_demand_on_spanning_forest(
    subgraph: PortGraph,
    terminal_demands: dict[str, float],
) -> dict[int, float]:
    """按末端需求沿生成森林路由流量，**保证每个设备的进出流量守恒**。

    为什么需要这个：原实现忽略 `terminal_demands`，把所有边一律设成
    `总需求 / 边数`。于是每个节点的残差恰好等于 `|入度 - 出度| × 均分流量`——
    实测示例项目冷冻水 111 条边、均分 0.184775 m³/h 时，8 个节点的残差
    （0.923874 / 0.554324 ×7）与"度数差 × 均分流量"**逐条精确吻合**。
    这与 Hardy-Cross 无关：把 Hardy-Cross 整个关掉，同样的 8 条错误照报，
    10 个内置样例里有 6 个在纯均分路径下也报（合计 16 条）。

    而这 8 条是 **error** 级 `conservation_violation`——兜底方案自己制造了
    假的"守恒违反"。同时 `solver.py` 的告警文案还写着"流量分配仍守恒"，
    与事实相反。改为沿生成森林反解后，连续性**按构造成立**（残差为浮点舍入量级），
    兜底不再产生任何假 error。

    做法：BFS 建生成森林 → 逆序遍历，子节点先定 → 父边流量由该节点的
    连续性方程反解。非树边（余树边）置 0：这是**一个**可行解而非平差解，
    符合它"兜底、仅供教学参考"的定位（真正的阻力平差由 Hardy-Cross 负责）。

    符号约定与 `_calculate_conservation_residuals` 一致：`edge_flow` 是沿
    `from_port → to_port` 方向的**带符号**流量。
    """
    edges = subgraph.edges

    adjacency: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for index, edge in enumerate(edges):
        from_id = edge.from_port.device_id
        to_id = edge.to_port.device_id
        adjacency[from_id].append((to_id, index))
        adjacency[to_id].append((from_id, index))

    edge_flow: dict[int, float] = {index: 0.0 for index in range(len(edges))}
    visited: set[str] = set()

    # 排序后遍历，保证分配结果与输入顺序无关（确定性）
    device_ids = sorted(adjacency)
    for root in device_ids:
        if root in visited:
            continue

        # 1. BFS 建树，记录每个节点的父边与子边
        visited.add(root)
        order: list[str] = []
        parent_edge: dict[str, int] = {}
        children: dict[str, list[tuple[str, int]]] = defaultdict(list)
        queue: deque[str] = deque([root])
        while queue:
            node = queue.popleft()
            order.append(node)
            for neighbour, index in sorted(adjacency[node], key=lambda item: (item[0], item[1])):
                if neighbour in visited:
                    continue
                visited.add(neighbour)
                parent_edge[neighbour] = index
                children[node].append((neighbour, index))
                queue.append(neighbour)

        # 2. 逆序回溯：子节点流量已知后，用连续性反解父边流量
        for node in reversed(order):
            parent_index = parent_edge.get(node)
            if parent_index is None:
                continue  # 根节点：源汇不平衡量由它吸收

            # 本节点自身需求 + 各子边带给它的净流入
            net = terminal_demands.get(node, 0.0)
            for _child, child_index in children[node]:
                child_edge = edges[child_index]
                if child_edge.to_port.device_id == node:
                    net += edge_flow[child_index]   # 子边流入本节点
                else:
                    net -= edge_flow[child_index]   # 子边流出本节点

            # 父边须抵消 net：连续性 Σ入 - Σ出 + 外部注入 = 0
            parent = edges[parent_index]
            if parent.to_port.device_id == node:
                edge_flow[parent_index] = -net   # 父 → 本节点
            else:
                edge_flow[parent_index] = net    # 本节点 → 父

    return edge_flow


def _calculate_conservation_residuals(
    subgraph: PortGraph,
    edge_flow: dict[int, float],
    terminal_demands: dict[str, float] | None = None,
) -> dict[str, float]:
    """计算守恒残差

    连续性方程**必须计入外部注入**，否则源/汇设备会被误判为违反守恒：

        residual = | Σ(入流) - Σ(出流) + 外部注入 |

    其中外部注入取自 `terminal_demands`（源 > 0、汇 < 0）。这与 Hardy-Cross 侧
    `bridge._calculate_conservation_residuals_from_hc` 的口径一致——两处口径必须
    相同，否则"兜底"会凭空报出一批守恒违反。

    历史教训：本函数原先只算 `|Σ入 - Σ出|` 且**完全忽略** `terminal_demands`，
    于是冷机这类源设备必然报出等于自身流量的残差。配合当时"按边数均分"的
    分配方式，示例项目一次性报出 8 条 `conservation_violation` **error**，
    而那 8 条与 Hardy-Cross 无关（关掉 Hardy-Cross 照报），纯属兜底自己制造。
    """
    device_inflow: dict[str, float] = defaultdict(float)
    device_outflow: dict[str, float] = defaultdict(float)

    for idx, edge in enumerate(subgraph.edges):
        flow = edge_flow.get(idx, 0.0)

        # from_port 出流，to_port 入流；edge_flow 是带符号的
        device_outflow[edge.from_port.device_id] += flow
        device_inflow[edge.to_port.device_id] += flow

    # 计算残差
    external = terminal_demands or {}
    all_devices = set(device_inflow) | set(device_outflow) | set(external)
    residuals: dict[str, float] = {}

    for device_id in all_devices:
        net = (
            device_inflow.get(device_id, 0.0)
            - device_outflow.get(device_id, 0.0)
            + external.get(device_id, 0.0)
        )
        residuals[device_id] = abs(net)

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
