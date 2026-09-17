"""Hardy Cross 流量求解器 - 核心迭代算法

实现 Hardy Cross 方法的迭代求解流程。
"""

from app.hardy_cross.models import Loop, NetworkModel, SolverConfig, SolverResult
from app.hardy_cross.resistance import (
    calculate_pressure_drop,
    calculate_resistance_derivative,
    initialize_edge_resistances,
)


def solve_hardy_cross(model: NetworkModel, config: SolverConfig) -> SolverResult:
    """Hardy Cross 迭代求解管网流量分配

    基本原理：
    1. 节点流量守恒：Σ Q_in = Σ Q_out
    2. 回路压降守恒：Σ ΔP = 0（沿闭合回路）

    修正公式：
    ΔQ = -Σ(ΔP) / Σ(|dΔP/dQ|)

    迭代格式是 **Gauss-Seidel**（逐回路算完立刻写回边流量），不是 Jacobi。
    这不是性能选择，而是**收敛性的必要条件**：Jacobi 把本轮所有回路的修正量
    一次性叠加，而本项目实测有 28/128 条边同时属于多个回路（最多一条属于 9 个
    回路），叠加后互相放大，迭代**几何发散**——实测示例项目冷冻水 15 个回路，
    max|ΔQ| 从 3.16 涨到 5.5e24；正规样例 `four_floor_fcu_fresh_air` 6 个回路
    同样涨到 6.8e61。改成逐回路写回后，两者都在 11~33 次迭代内收敛，
    且收敛到与强阻尼 Jacobi 完全相同的解（边流量最大差 ~1e-8 m³/h，即机器精度）。

    Args:
        model: 管网模型
        config: 求解器配置

    Returns:
        求解结果
    """
    # 预计算阻力系数
    initialize_edge_resistances(model, config)

    # 初始化流量（简单均分）
    _initialize_flows(model, config)

    # 迭代求解
    max_correction = 0.0
    for iteration in range(config.max_iterations):
        max_correction = 0.0

        # 逐回路：算出修正量后**立刻**写回边流量，后续回路基于已更新的流量计算。
        # 这样同一轮内先处理的回路已经改进了解，避免 Jacobi 式过冲发散。
        for loop in model.loops:
            correction = _calculate_loop_correction(loop, model, config)
            loop.correction_m3_h = correction
            max_correction = max(max_correction, abs(correction))
            _apply_loop_correction(model, loop, correction)

        # 检查收敛
        if max_correction < config.tolerance_m3_h:
            return SolverResult(
                converged=True,
                iterations=iteration + 1,
                max_residual_m3_h=max_correction,
                edge_flows={e.id: e.flow_m3_h for e in model.edges.values()},
            )

    # 未收敛
    return SolverResult(
        converged=False,
        iterations=config.max_iterations,
        max_residual_m3_h=max_correction,
        edge_flows={e.id: e.flow_m3_h for e in model.edges.values()},
    )


def _initialize_flows(model: NetworkModel, config: SolverConfig):
    """初始化边流量（必须满足节点连续性）

    Hardy Cross 的回路修正只在闭合回路上加减同一个 ΔQ，因此**不会**改变
    节点连续性残差。初始猜测如果不满足 Σ(入) - Σ(出) + 外部注入 = 0，
    迭代结束后残差依然存在。

    策略（生成树法）：
    1. 求生成森林，把边分为树边与非树边（余树边）
    2. 非树边流量置 0（初始猜测，后续由回路修正分配）
    3. 按深度从大到小回溯，用节点连续性反解每条树边流量

    符号约定：flow_m3_h > 0 表示沿 from_node → to_node 流动。
    """
    tree_edges, parent_edge_of, order = _spanning_forest(model)

    # 1. 全部置零：非树边保持 0 作为初始猜测，树边随后被反解
    for edge in model.edges.values():
        edge.flow_m3_h = 0.0

    resolved: set[str] = {edge_id for edge_id in model.edges if edge_id not in tree_edges}

    # 2. 从最深节点回溯，逐个反解父边流量
    for node_id in reversed(order):
        parent_edge_id = parent_edge_of.get(node_id)
        if parent_edge_id is None:
            continue  # 根节点：连续性由整体平衡保证

        node = model.nodes[node_id]
        net = node.external_flow_m3_h

        for edge in model.get_connected_edges(node_id):
            if edge.id == parent_edge_id or edge.id not in resolved:
                continue
            if edge.to_node == node_id:
                net += edge.flow_m3_h  # 入流
            if edge.from_node == node_id:
                net -= edge.flow_m3_h  # 出流

        parent_edge = model.edges[parent_edge_id]
        if parent_edge.to_node == node_id:
            # 父边流入本节点：net + f = 0
            parent_edge.flow_m3_h = -net
        else:
            # 父边流出本节点：net - f = 0
            parent_edge.flow_m3_h = net

        resolved.add(parent_edge_id)

    # 3. 全零流量（无外部需求）时给一个非零初值，避免导数退化
    if all(abs(edge.flow_m3_h) < 1e-12 for edge in model.edges.values()):
        for edge in model.edges.values():
            edge.flow_m3_h = config.initial_flow_m3_h


def _spanning_forest(model: NetworkModel) -> tuple[set[str], dict[str, str], list[str]]:
    """构建生成森林（无向 BFS）

    Returns:
        (树边ID集合, {node_id: 父边ID}, BFS 访问顺序)
    """
    from collections import deque

    adjacency: dict[str, list[tuple[str, str]]] = {node_id: [] for node_id in model.nodes}
    for edge in model.edges.values():
        adjacency.setdefault(edge.from_node, []).append((edge.id, edge.to_node))
        adjacency.setdefault(edge.to_node, []).append((edge.id, edge.from_node))

    tree_edges: set[str] = set()
    parent_edge_of: dict[str, str] = {}
    order: list[str] = []
    visited: set[str] = set()

    for root in sorted(adjacency):
        if root in visited:
            continue
        visited.add(root)
        order.append(root)
        queue = deque([root])
        while queue:
            current = queue.popleft()
            for edge_id, neighbor in sorted(adjacency.get(current, [])):
                if neighbor in visited:
                    continue
                visited.add(neighbor)
                tree_edges.add(edge_id)
                parent_edge_of[neighbor] = edge_id
                order.append(neighbor)
                queue.append(neighbor)

    return tree_edges, parent_edge_of, order


def _calculate_loop_correction(loop: Loop, model: NetworkModel, config: SolverConfig) -> float:
    """计算回路的流量修正量

    修正公式：
    ΔQ = -Σ(ΔP) / Σ(|dΔP/dQ|)

    其中：
    - 分子：回路总压降（考虑方向）
    - 分母：回路阻力导数总和（取绝对值）

    Args:
        loop: 回路
        model: 管网模型
        config: 求解器配置

    Returns:
        流量修正量（m³/h）
    """
    numerator = 0.0    # Σ(ΔP)
    denominator = 0.0  # Σ(|dΔP/dQ|)

    for edge_id, direction in zip(loop.edges, loop.directions):
        edge = model.edges[edge_id]

        # 考虑边在回路中的方向
        Q = edge.flow_m3_h * direction

        # 压降（考虑流向）
        delta_P = calculate_pressure_drop(edge, Q, config)
        numerator += delta_P

        # 阻力导数（总是正值）
        derivative = calculate_resistance_derivative(edge, Q, config)
        denominator += abs(derivative)

    # 修正量
    if abs(denominator) < 1e-9:
        return 0.0

    correction = -numerator / denominator
    return correction


def _apply_loop_correction(model: NetworkModel, loop: Loop, correction: float) -> None:
    """把单个回路的修正量写回它经过的每条边。

    回路修正只改变**回路内部**的流量分配，不改变各节点的净进出流量，因此
    节点连续性残差始终保持在浮点舍入量级（实测相对残差 ~1e-16）——这也是
    为什么"守恒残差"不能用来判断 Hardy-Cross 结果是否可信，详见
    `app/hardy_cross/bridge.py` 的 `_allocation_is_usable`。
    """
    for edge_id, direction in zip(loop.edges, loop.directions):
        model.edges[edge_id].flow_m3_h += correction * direction


def _apply_corrections(model: NetworkModel):
    """把**全部**回路的修正量一次性写回各边（Jacobi 式）。

    .. deprecated::
        保留仅为兼容既有测试与外部调用。该格式在多回路管网上会几何发散
        （见 `solve_hardy_cross` 的说明），新代码请用 `_apply_loop_correction`。
    """
    edge_corrections: dict[str, float] = {edge_id: 0.0 for edge_id in model.edges}

    for loop in model.loops:
        for edge_id, direction in zip(loop.edges, loop.directions):
            edge_corrections[edge_id] += loop.correction_m3_h * direction

    for edge_id, correction in edge_corrections.items():
        model.edges[edge_id].flow_m3_h += correction
