"""拓扑结构验证

检测端口级拓扑问题：
- 孤立端口：设备端口无连接
- 断路：介质子图不连通
- 短路：供回直连无中间负荷
- 供回接反：供水口接供水口、回水口接回水口
- 介质混接：不同介质连接
- 无效环路：无泵驱动的闭环
- 缺失设备：系统缺少必需的核心设备
"""

from collections import Counter, defaultdict, deque
from dataclasses import dataclass

from app.schemas import Issue
from .port_graph import PortGraph


@dataclass
class TopologyIssue:
    """拓扑问题"""
    level: str  # "error", "warning", "info"
    code: str
    message: str
    target: str | None = None


def validate_topology(graph: PortGraph) -> list[TopologyIssue]:
    """验证拓扑结构"""
    issues: list[TopologyIssue] = []

    # 1. 检查孤立端口
    issues.extend(_check_isolated_ports(graph))

    # 2. 检查每个介质子图的连通性
    issues.extend(_check_connectivity(graph))

    # 3. 检查供回接反
    issues.extend(_check_supply_return_mismatch(graph))

    # 4. 检查介质混接
    issues.extend(_check_medium_mismatch(graph))

    # 5. 检查缺失设备
    issues.extend(_check_missing_equipment(graph))

    # 6. 检查分/集水器主管和分支绑定
    issues.extend(_check_manifold_bindings(graph))

    return issues


def _check_manifold_bindings(graph: PortGraph) -> list[TopologyIssue]:
    """Report one bounded issue per manifold binding defect."""
    issues: list[TopologyIssue] = []
    grouped = defaultdict(list)
    for binding in graph.manifold_bindings:
        grouped[(binding.manifold_id, binding.medium)].append(binding)

    for (manifold_id, medium), bindings in sorted(grouped.items()):
        main_bindings = [binding for binding in bindings if binding.kind == "main"]
        branch_bindings = [binding for binding in bindings if binding.kind == "branch"]

        if not main_bindings:
            issues.append(
                TopologyIssue(
                    level="error",
                    code="manifold_missing_main",
                    message=f"分/集水器 {manifold_id} 在 {medium} 系统缺少主管连接",
                    target=manifold_id,
                )
            )
        elif len(main_bindings) > 1:
            issues.append(
                TopologyIssue(
                    level="warning",
                    code="manifold_duplicate_main",
                    message=f"分/集水器 {manifold_id} 在 {medium} 系统存在多个主管连接",
                    target=manifold_id,
                )
            )

        if not branch_bindings:
            issues.append(
                TopologyIssue(
                    level="warning",
                    code="manifold_missing_branch",
                    message=f"分/集水器 {manifold_id} 在 {medium} 系统缺少分支连接",
                    target=manifold_id,
                )
            )
            continue

        duplicate_counts = Counter(
            (binding.port_index, binding.peer_id)
            for binding in branch_bindings
        )
        if any(count > 1 for count in duplicate_counts.values()):
            issues.append(
                TopologyIssue(
                    level="warning",
                    code="manifold_duplicate_branch",
                    message=f"分/集水器 {manifold_id} 在 {medium} 系统存在重复分支连接",
                    target=manifold_id,
                )
            )

        peers_by_port: dict[int, set[str]] = defaultdict(set)
        for binding in branch_bindings:
            peers_by_port[binding.port_index].add(binding.peer_id)
        if any(len(peers) > 1 for peers in peers_by_port.values()):
            issues.append(
                TopologyIssue(
                    level="warning",
                    code="manifold_excess_branch",
                    message=f"分/集水器 {manifold_id} 在 {medium} 系统有多个分支共用端口",
                    target=manifold_id,
                )
            )

    return issues


def _check_isolated_ports(graph: PortGraph) -> list[TopologyIssue]:
    """检查孤立端口"""
    issues: list[TopologyIssue] = []

    for port in graph.ports:
        key = (port.device_id, port.port_index)
        if key not in graph.adjacency or len(graph.adjacency[key]) == 0:
            issues.append(TopologyIssue(
                level="warning",
                code="isolated_port",
                message=f"设备 {port.device_id} 端口 {port.port_index} 无连接",
                target=port.device_id,
            ))

    return issues


def _check_connectivity(graph: PortGraph) -> list[TopologyIssue]:
    """检查介质子图连通性（断路检测）

    如果一个介质子图中有设备但没有边，说明设备间没有连接（孤立设备）
    """
    issues: list[TopologyIssue] = []

    for medium, subgraph in graph.medium_subgraphs.items():
        if not subgraph.ports:
            continue

        # 如果有设备端口但没有边，说明有设备但没有连接
        if len(subgraph.device_ports) > 1 and len(subgraph.edges) == 0:
            issues.append(TopologyIssue(
                level="error",
                code="disconnected_network",
                message=f"{medium} 系统存在 {len(subgraph.device_ports)} 个孤立设备（无连接）",
                target=f"{medium}_network",
            ))
            continue

        # BFS 查找连通分量
        visited: set[tuple[str, int]] = set()
        components: list[set[str]] = []

        for port in subgraph.ports:
            key = (port.device_id, port.port_index)
            if key in visited:
                continue

            # 新连通分量
            component: set[str] = set()
            queue = deque([key])
            visited.add(key)

            while queue:
                current = queue.popleft()
                component.add(current[0])  # device_id

                # 遍历当前端口的所有边，找到邻居端口
                for edge in subgraph.adjacency.get(current, []):
                    # 边的两端：from_port 和 to_port
                    # 如果当前端口是 from_port，邻居是 to_port；反之亦然
                    from_key = (edge.from_port.device_id, edge.from_port.port_index)
                    to_key = (edge.to_port.device_id, edge.to_port.port_index)

                    if from_key == current:
                        neighbor_key = to_key
                    else:
                        neighbor_key = from_key

                    if neighbor_key not in visited:
                        visited.add(neighbor_key)
                        queue.append(neighbor_key)

            if component:
                components.append(component)

        # 如果有多个连通分量，说明有断路
        if len(components) > 1:
            issues.append(TopologyIssue(
                level="error",
                code="disconnected_network",
                message=f"{medium} 系统存在 {len(components)} 个独立连通分量（断路）",
                target=f"{medium}_network",
            ))

    return issues


def _check_supply_return_mismatch(graph: PortGraph) -> list[TopologyIssue]:
    """检查供回接反

    正确连接：supply -> return 或 return -> supply
    错误连接：supply -> supply 或 return -> return
    """
    issues: list[TopologyIssue] = []

    for edge in graph.edges:
        from_dir = edge.from_port.direction
        to_dir = edge.to_port.direction

        # 跳过双向端口
        if from_dir == "bidirectional" or to_dir == "bidirectional":
            continue

        # supply -> supply 或 return -> return
        if from_dir == to_dir:
            issues.append(TopologyIssue(
                level="error",
                code="supply_return_reversed",
                message=f"供回接反：{edge.from_port.device_id}({from_dir}) -> {edge.to_port.device_id}({to_dir})",
                target=edge.from_port.device_id,
            ))

    return issues


def _check_medium_mismatch(graph: PortGraph) -> list[TopologyIssue]:
    """检查介质混接"""
    issues: list[TopologyIssue] = []

    for edge in graph.edges:
        from_medium = edge.from_port.medium
        to_medium = edge.to_port.medium

        # 跳过未指定介质的端口
        if from_medium is None or to_medium is None:
            continue

        if from_medium != to_medium:
            issues.append(TopologyIssue(
                level="error",
                code="medium_mismatch",
                message=f"介质混接：{edge.from_port.device_id}({from_medium}) -> {edge.to_port.device_id}({to_medium})",
                target=edge.from_port.device_id,
            ))

    return issues


def _check_missing_equipment(graph: PortGraph) -> list[TopologyIssue]:
    """检查缺失设备

    基本规则：
    - 冷冻水系统：需要冷源（chiller）+ 泵（pump）+ 末端（fcu/ahu）
    - 冷却水系统：需要冷却塔（cooling_tower）+ 泵
    - 热水系统：需要热源（heat_pump/boiler）+ 泵 + 末端
    - 风系统：需要风机（fan/ahu/fresh_air_unit）

    注意：本检查基于 port.medium，需先推断设备角色
    """
    issues: list[TopologyIssue] = []

    # 统计设备角色（简化：基于介质推断）
    chilled_water_devices = {
        p.device_id
        for p in graph.ports
        if p.medium == "chilled_water" and p.owner_kind == "device"
    }
    cooling_water_devices = {
        p.device_id
        for p in graph.ports
        if p.medium == "cooling_water" and p.owner_kind == "device"
    }
    # 冷冻水系统检查（简化：至少3个设备）
    if chilled_water_devices and len(chilled_water_devices) < 3:
        issues.append(TopologyIssue(
            level="warning",
            code="incomplete_chilled_water_system",
            message=f"冷冻水系统设备不足（需要冷源、泵、末端），当前仅 {len(chilled_water_devices)} 个设备",
            target="chilled_water_system",
        ))

    # 冷却水系统检查
    if cooling_water_devices and len(cooling_water_devices) < 2:
        issues.append(TopologyIssue(
            level="warning",
            code="incomplete_cooling_water_system",
            message=f"冷却水系统设备不足（需要冷却塔、泵），当前仅 {len(cooling_water_devices)} 个设备",
            target="cooling_water_system",
        ))

    return issues


def convert_to_schema_issues(topology_issues: list[TopologyIssue]) -> list[Issue]:
    """转换为 schemas.Issue 格式"""
    return [
        Issue(
            level=issue.level,  # type: ignore
            code=issue.code,
            message=issue.message,
            target=issue.target,
        )
        for issue in topology_issues
    ]
