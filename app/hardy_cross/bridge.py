"""Hardy Cross 求解器与稳态求解器的集成桥接

将端口图转换为 Hardy Cross 网络模型，并将求解结果映射回端口图格式。
"""

from collections import defaultdict, deque

from app.hardy_cross import (
    NetworkModel,
    NetworkNode,
    PipeEdge,
    SolverConfig,
    detect_loops,
    solve_hardy_cross,
)
from app.steady_solver.flow_solver import FlowAllocation
from app.steady_solver.port_graph import Medium, PortGraph


def allocate_flow_hardy_cross(
    graph: PortGraph,
    medium: Medium,
    total_demand_m3_h: float,
    terminal_demands: dict[str, float],
) -> FlowAllocation:
    """使用 Hardy Cross 方法进行流量分配

    Args:
        graph: 端口图
        medium: 介质类型
        total_demand_m3_h: 总需求流量（m³/h）
        terminal_demands: 设备流量需求 {device_id: flow_m3_h}
            - 正值表示源设备（注入流量）
            - 负值表示汇设备（消耗流量）
            - 必须满足: sum(terminal_demands.values()) ≈ 0

    Returns:
        FlowAllocation 包含端口流量、边流量和守恒残差
    """
    if not terminal_demands:
        raise ValueError(
            "terminal_demands is required. Specify flow for each device: "
            "positive for sources (e.g. chiller), negative for sinks (e.g. FCU)"
        )

    subgraph = graph.medium_subgraphs.get(medium)

    if not subgraph or not subgraph.edges:
        return FlowAllocation(
            port_flow={},
            edge_flow={},
            conservation_residuals={},
            total_demand=total_demand_m3_h,
        )

    # A manifold circuit already carries an explicit supply/return direction in
    # its connection rows. Route each terminal's design flow around that closed
    # circuit before falling back to the undirected pressure solver. This keeps
    # device-internal zero-length edges from becoming hydraulic shortcuts.
    if subgraph.manifold_bindings:
        directed_allocation = _allocate_directed_manifold_flow(
            subgraph,
            total_demand_m3_h,
            terminal_demands,
        )
        if directed_allocation is not None:
            return directed_allocation

    # 1. 转换为 Hardy Cross 网络模型
    hc_model = _convert_to_hardy_cross_model(subgraph, medium, total_demand_m3_h, terminal_demands)

    # 2. 检测回路
    hc_model.loops = detect_loops(hc_model)

    # 3. Hardy Cross 求解
    config = SolverConfig(
        max_iterations=50,
        tolerance_m3_h=0.01,
        initial_flow_m3_h=1.0,
    )
    result = solve_hardy_cross(hc_model, config)

    # 4. 转换结果回端口图格式
    return _convert_back_to_flow_allocation(subgraph, hc_model, result, total_demand_m3_h)


def _allocate_directed_manifold_flow(
    subgraph: PortGraph,
    total_demand_m3_h: float,
    terminal_demands: dict[str, float],
) -> FlowAllocation | None:
    """Route design flow through directed supply and return paths.

    Positive demand rows identify sources and negative rows identify terminals.
    Each source-to-terminal route is paired with a terminal-to-source route, so
    the result represents circulation in a closed water loop rather than mass
    disappearing at the terminal.
    """
    tolerance = 0.01
    sources = {
        device_id: flow
        for device_id, flow in sorted(terminal_demands.items())
        if flow > tolerance
    }
    sinks = {
        device_id: -flow
        for device_id, flow in sorted(terminal_demands.items())
        if flow < -tolerance
    }
    if not sources or not sinks:
        return None
    if abs(sum(sources.values()) - sum(sinks.values())) > tolerance:
        return None

    adjacency: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for edge_index, edge in enumerate(subgraph.edges):
        from_owner = edge.from_port.device_id
        to_owner = edge.to_port.device_id
        if from_owner != to_owner:
            adjacency[from_owner].append((to_owner, edge_index))
    for routes in adjacency.values():
        routes.sort(key=lambda item: (item[0], item[1]))

    demand_devices = set(sources) | set(sinks)
    source_remaining = dict(sources)
    edge_flow = {index: 0.0 for index in range(len(subgraph.edges))}

    for sink_id, sink_flow in sinks.items():
        sink_remaining = sink_flow
        for source_id in sorted(source_remaining):
            available = source_remaining[source_id]
            if available <= tolerance:
                continue

            blocked = demand_devices - {source_id, sink_id}
            supply_path = _find_directed_owner_path(
                adjacency,
                source_id,
                sink_id,
                blocked,
            )
            return_path = _find_directed_owner_path(
                adjacency,
                sink_id,
                source_id,
                blocked,
            )
            if supply_path is None or return_path is None:
                continue

            routed_flow = min(available, sink_remaining)
            for edge_index in supply_path + return_path:
                edge_flow[edge_index] += routed_flow
            source_remaining[source_id] -= routed_flow
            sink_remaining -= routed_flow
            if sink_remaining <= tolerance:
                break

        if sink_remaining > tolerance:
            return None

    if any(flow > tolerance for flow in source_remaining.values()):
        return None

    if not _assign_physical_pipe_flows(subgraph, edge_flow, tolerance):
        return None

    port_flow: dict[tuple[str, int], float] = defaultdict(float)
    device_net_inflow: dict[str, float] = defaultdict(float)
    for edge_index, edge in enumerate(subgraph.edges):
        flow = edge_flow[edge_index]
        from_key = (edge.from_port.device_id, edge.from_port.port_index)
        to_key = (edge.to_port.device_id, edge.to_port.port_index)
        port_flow[from_key] += abs(flow)
        port_flow[to_key] += abs(flow)
        device_net_inflow[edge.from_port.device_id] -= flow
        device_net_inflow[edge.to_port.device_id] += flow

    residuals = {
        device_id: abs(device_net_inflow.get(device_id, 0.0))
        for device_id in sorted(subgraph.device_ports)
    }
    return FlowAllocation(
        port_flow=dict(port_flow),
        edge_flow=edge_flow,
        conservation_residuals=residuals,
        total_demand=total_demand_m3_h,
    )


def _find_directed_owner_path(
    adjacency: dict[str, list[tuple[str, int]]],
    start: str,
    target: str,
    blocked: set[str],
) -> list[int] | None:
    """Return one deterministic directed path as subgraph edge indexes."""
    queue = deque([start])
    parent: dict[str, tuple[str, int] | None] = {start: None}

    while queue:
        owner_id = queue.popleft()
        if owner_id == target:
            break
        for neighbor_id, edge_index in adjacency.get(owner_id, []):
            if neighbor_id in blocked or neighbor_id in parent:
                continue
            parent[neighbor_id] = (owner_id, edge_index)
            queue.append(neighbor_id)

    if target not in parent:
        return None

    path: list[int] = []
    current = target
    while True:
        parent_step = parent[current]
        if parent_step is None:
            break
        previous, edge_index = parent_step
        path.append(edge_index)
        current = previous
    path.reverse()
    return path


def _assign_physical_pipe_flows(
    subgraph: PortGraph,
    edge_flow: dict[int, float],
    tolerance: float,
) -> bool:
    """Copy each routed pipe owner's through-flow onto its physical edge."""
    for physical_index, physical_edge in enumerate(subgraph.edges):
        pipe_id = physical_edge.pipe_id
        if pipe_id is None:
            continue

        inbound = 0.0
        outbound = 0.0
        for edge_index, edge in enumerate(subgraph.edges):
            if edge_index == physical_index:
                continue
            from_owner = edge.from_port.device_id
            to_owner = edge.to_port.device_id
            if to_owner == pipe_id and from_owner != pipe_id:
                inbound += edge_flow[edge_index]
            if from_owner == pipe_id and to_owner != pipe_id:
                outbound += edge_flow[edge_index]

        if abs(inbound - outbound) > tolerance:
            return False
        edge_flow[physical_index] = (inbound + outbound) / 2.0

    return True


def _convert_to_hardy_cross_model(
    subgraph: PortGraph,
    medium: Medium,
    total_demand_m3_h: float,
    terminal_demands: dict[str, float],
) -> NetworkModel:
    """将端口图转换为 Hardy Cross 网络模型

    Args:
        subgraph: 端口子图
        medium: 介质类型
        total_demand_m3_h: 总需求
        terminal_demands: 末端需求

    Returns:
        NetworkModel
    """
    nodes: dict[str, NetworkNode] = {}
    edges: dict[str, PipeEdge] = {}

    # 1. 为每个端口创建节点
    for device_id, ports in subgraph.device_ports.items():
        for port in ports:
            node_id = f"{device_id}:{port.port_index}"

            # 从 terminal_demands 获取外部流量（源/汇）
            # 正值 = 源（注入），负值 = 汇（消耗）
            device_demand = terminal_demands.get(device_id, 0.0)
            external_flow = device_demand / len(ports)  # 平均分配到设备的所有端口

            nodes[node_id] = NetworkNode(
                id=node_id,
                device_id=device_id,
                port_type=port.direction,
                external_flow_m3_h=external_flow,
            )

    # 2. 为每条边创建管道
    for idx, edge in enumerate(subgraph.edges):
        edge_id = f"edge_{idx}"
        from_node_id = f"{edge.from_port.device_id}:{edge.from_port.port_index}"
        to_node_id = f"{edge.to_port.device_id}:{edge.to_port.port_index}"

        # 获取管道参数
        diameter_mm = edge.diameter_mm or 100.0  # 默认 100mm
        length_m = edge.length_m

        edges[edge_id] = PipeEdge(
            id=edge_id,
            from_node=from_node_id,
            to_node=to_node_id,
            diameter_mm=diameter_mm,
            length_m=length_m,
        )

    # Device ports belong to the same hydraulic component. These zero-length
    # edges preserve port-level nodes while allowing continuity through pumps,
    # sources, and terminals. They are solver-only and are not exposed as
    # connection flows in the returned allocation.
    physical_pipe_ids = {edge.pipe_id for edge in subgraph.edges if edge.pipe_id}
    for device_id in sorted(subgraph.device_ports):
        if device_id in physical_pipe_ids:
            continue
        device_ports = sorted(
            subgraph.device_ports[device_id],
            key=lambda port: port.port_index,
        )
        if len(device_ports) < 2:
            continue
        anchor_node_id = f"{device_id}:{device_ports[0].port_index}"
        for port in device_ports[1:]:
            edge_id = f"internal:{device_id}:{port.port_index}"
            edges[edge_id] = PipeEdge(
                id=edge_id,
                from_node=anchor_node_id,
                to_node=f"{device_id}:{port.port_index}",
                diameter_mm=100.0,
                length_m=0.0,
            )

    return NetworkModel(
        nodes=nodes,
        edges=edges,
        loops=[],
        medium=medium,  # medium 已经是字符串，不需要 .value
    )


def _convert_back_to_flow_allocation(
    subgraph: PortGraph,
    hc_model: NetworkModel,
    result,
    total_demand: float,
) -> FlowAllocation:
    """将 Hardy Cross 求解结果转换回 FlowAllocation 格式

    Args:
        subgraph: 原始端口子图
        hc_model: Hardy Cross 模型
        result: Hardy Cross 求解结果
        total_demand: 总需求

    Returns:
        FlowAllocation
    """
    port_flow: dict[tuple[str, int], float] = defaultdict(float)
    edge_flow: dict[int, float] = {}

    # 1. 映射边流量（保留符号：负值表示逆 from_port → to_port 方向流动）
    for idx, edge in enumerate(subgraph.edges):
        edge_id = f"edge_{idx}"
        flow = result.edge_flows.get(edge_id, 0.0)
        edge_flow[idx] = flow

        # 端口流量取绝对值（流经该端口的流量大小）
        from_key = (edge.from_port.device_id, edge.from_port.port_index)
        to_key = (edge.to_port.device_id, edge.to_port.port_index)
        port_flow[from_key] += abs(flow)
        port_flow[to_key] += abs(flow)

    # 2. 计算守恒残差（需计入外部注入）
    conservation_residuals = _calculate_conservation_residuals_from_hc(
        subgraph, edge_flow, hc_model
    )

    return FlowAllocation(
        port_flow=dict(port_flow),
        edge_flow=edge_flow,
        conservation_residuals=conservation_residuals,
        total_demand=total_demand,
    )


def _calculate_conservation_residuals_from_hc(
    subgraph: PortGraph,
    edge_flow: dict[int, float],
    hc_model: NetworkModel,
) -> dict[str, float]:
    """从 Hardy Cross 结果计算守恒残差

    连续性方程必须计入外部注入，否则源设备（外部注入 > 0）会被误判为违反守恒：

        residual = | Σ(入流) - Σ(出流) + 外部注入 |

    注意 edge_flow 是**带符号**的：负值表示实际流向与 from_port → to_port 相反，
    此处直接按符号累加，不能取绝对值。
    """
    device_net_inflow: dict[str, float] = defaultdict(float)

    for idx, edge in enumerate(subgraph.edges):
        flow = edge_flow.get(idx, 0.0)

        # from_port 侧为出流（-），to_port 侧为入流（+）
        device_net_inflow[edge.from_port.device_id] -= flow
        device_net_inflow[edge.to_port.device_id] += flow

    # 汇总每个设备的外部注入（源 > 0，汇 < 0）
    device_external: dict[str, float] = defaultdict(float)
    for node in hc_model.nodes.values():
        device_external[node.device_id] += node.external_flow_m3_h

    all_devices = set(device_net_inflow) | set(device_external)
    residuals: dict[str, float] = {}

    for device_id in all_devices:
        net_inflow = device_net_inflow.get(device_id, 0.0)
        external = device_external.get(device_id, 0.0)
        residuals[device_id] = abs(net_inflow + external)

    return residuals
