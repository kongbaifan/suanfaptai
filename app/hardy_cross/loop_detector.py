"""Hardy Cross 流量求解器 - 回路检测

使用深度优先搜索检测管网中的所有基本回路。
"""

from app.hardy_cross.models import Loop, NetworkModel, PipeEdge


def detect_loops(model: NetworkModel) -> list[Loop]:
    """检测管网中的所有基本回路

    使用多种策略检测不同类型的回路：
    1. DFS 检测一般回路
    2. 特殊处理并联管道（两条边连接相同节点对）

    Args:
        model: 管网模型

    Returns:
        回路列表
    """
    if not model.edges:
        return []

    loops: list[Loop] = []

    # 策略 1: 检测并联管道（特殊情况）
    loops.extend(_detect_parallel_loops(model))

    # 策略 2: DFS 检测一般回路
    loops.extend(_detect_dfs_loops(model, loops))

    return loops


def _detect_parallel_loops(model: NetworkModel) -> list[Loop]:
    """检测并联管道形成的回路

    真正的并联管道：多条边有相同的 from_node 和 to_node（方向一致）
    注意：只有方向相同的边才能形成并联回路
    """
    loops: list[Loop] = []
    # 按 (from_node, to_node) 有序对分组
    directed_pair_edges: dict[tuple[str, str], list[str]] = {}

    for edge_id, edge in model.edges.items():
        pair = (edge.from_node, edge.to_node)
        if pair not in directed_pair_edges:
            directed_pair_edges[pair] = []
        directed_pair_edges[pair].append(edge_id)

    # 找到真正的并联管道（方向相同的多条边）
    for pair, edge_ids in directed_pair_edges.items():
        if len(edge_ids) >= 2:
            # 每两条并联边形成一个回路
            for i in range(len(edge_ids) - 1):
                for j in range(i + 1, len(edge_ids)):
                    edge1_id = edge_ids[i]
                    edge2_id = edge_ids[j]
                    loop = Loop(
                        id=f"loop_parallel_{len(loops) + 1}",
                        edges=[edge1_id, edge2_id],
                        directions=[1, -1],  # edge1 正向通过，edge2 反向返回
                    )
                    loops.append(loop)

    return loops


def _detect_dfs_loops(model: NetworkModel, existing_loops: list[Loop]) -> list[Loop]:
    """使用 DFS 检测一般回路（非并联）

    Args:
        model: 管网模型
        existing_loops: 已检测到的回路（用于去重）

    Returns:
        新检测到的回路列表
    """
    # 构建邻接表
    adj_list = _build_adjacency_list(model)

    # DFS 搜索回路
    loops: list[Loop] = []
    visited = set()
    rec_stack = []

    def dfs(node_id: str, parent_edge_id: str | None = None):
        visited.add(node_id)
        rec_stack.append(node_id)

        # 遍历所有相邻边
        for edge_id, neighbor_id, direction in adj_list.get(node_id, []):
            # 跳过父边（避免立即返回）
            if edge_id == parent_edge_id:
                continue

            if neighbor_id in rec_stack:
                # 找到回路！
                loop_start_idx = rec_stack.index(neighbor_id)
                loop_nodes = rec_stack[loop_start_idx:] + [neighbor_id]

                # 至少需要 2 个节点（形成闭环）
                if len(loop_nodes) >= 2:
                    loop = _build_loop_from_nodes(loop_nodes, model, existing_loops + loops)
                    if loop and _is_valid_loop(loop):
                        loops.append(loop)
            elif neighbor_id not in visited:
                dfs(neighbor_id, edge_id)

        rec_stack.pop()

    # 从每个未访问节点开始 DFS
    for node_id in model.nodes:
        if node_id not in visited:
            dfs(node_id)

    return loops


def _is_valid_loop(loop: Loop) -> bool:
    """检查回路是否有效

    有效回路：
    1. 至少包含 2 条边
    2. 所有边都不相同（没有重复边）
    """
    if len(loop.edges) < 2:
        return False

    # 检查是否有重复边
    if len(set(loop.edges)) != len(loop.edges):
        return False

    return True


def _build_adjacency_list(model: NetworkModel) -> dict[str, list[tuple[str, str, int]]]:
    """构建邻接表

    Returns:
        {node_id: [(edge_id, neighbor_id, direction), ...]}
        direction: 1=正向（from→to），-1=反向（to→from）
    """
    adj_list: dict[str, list[tuple[str, str, int]]] = {}

    for edge_id, edge in model.edges.items():
        # 正向：from_node → to_node
        if edge.from_node not in adj_list:
            adj_list[edge.from_node] = []
        adj_list[edge.from_node].append((edge_id, edge.to_node, 1))

        # 反向：to_node → from_node（无向图）
        if edge.to_node not in adj_list:
            adj_list[edge.to_node] = []
        adj_list[edge.to_node].append((edge_id, edge.from_node, -1))

    return adj_list


def _build_loop_from_nodes(
    loop_nodes: list[str],
    model: NetworkModel,
    existing_loops: list[Loop]
) -> Loop | None:
    """从节点列表构建回路

    Args:
        loop_nodes: 回路节点列表（首尾相同）
        model: 管网模型
        existing_loops: 已存在的回路（用于去重）

    Returns:
        Loop 或 None（如果找不到边或回路已存在）
    """
    if len(loop_nodes) < 2:  # 至少需要形成闭环
        return None

    edges: list[str] = []
    directions: list[int] = []
    used_edges: set[str] = set()

    # 找到连接相邻节点的边（避免重复使用同一条边）
    for i in range(len(loop_nodes) - 1):
        from_node = loop_nodes[i]
        to_node = loop_nodes[i + 1]

        edge, direction = _find_edge_between(from_node, to_node, model, used_edges)
        if edge is None or direction is None:
            return None  # 找不到边，跳过此回路

        edges.append(edge.id)
        directions.append(direction)
        used_edges.add(edge.id)

    # 检查是否与已存在回路重复（边集合相同）
    edge_set = set(edges)
    for existing in existing_loops:
        if set(existing.edges) == edge_set:
            return None  # 重复回路

    loop_id = f"loop_{len(existing_loops) + 1}"
    return Loop(id=loop_id, edges=edges, directions=directions)


def _find_edge_between(
    from_node: str,
    to_node: str,
    model: NetworkModel,
    used_edges: set[str] | None = None
) -> tuple[PipeEdge, int] | tuple[None, None]:
    """找到连接两个节点的边（优先选择未使用的边）

    Args:
        from_node: 起始节点
        to_node: 终止节点
        model: 管网模型
        used_edges: 已使用的边ID集合（用于避免重复使用同一条边）

    Returns:
        (edge, direction) 或 (None, None)
        direction: 1=正向，-1=反向
    """
    if used_edges is None:
        used_edges = set()

    for edge in model.edges.values():
        if edge.id in used_edges:
            continue  # 跳过已使用的边

        if edge.from_node == from_node and edge.to_node == to_node:
            return edge, 1  # 正向
        if edge.from_node == to_node and edge.to_node == from_node:
            return edge, -1  # 反向

    return None, None
