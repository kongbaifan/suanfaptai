"""端口级拓扑图构建

从 SystemEvaluateRequest 构建端口级连接图，支持：
- 设备端口枚举（基于 unity_catalog 角色）
- 连接方向、介质、尺寸、有效性
- 楼层信息
- 介质子图分离
"""

from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Literal

from app.data.equipment import role_for_device, spec_for_device
from app.schemas import DeviceInput, PipeInput, SystemEvaluateRequest
from app.unity_catalog import DeviceRole, find_equipment_spec, get_device_role


Medium = Literal["chilled_water", "cooling_water", "hot_water", "air"]

MANIFOLD_DEVICE_TYPES = {"分水器", "集水器"}
MANIFOLD_MAIN_PIPE_TYPES = {
    "main_supply",
    "main_return",
    "riser_supply",
    "riser_return",
}
MANIFOLD_BRANCH_PIPE_TYPES = {
    "branch_supply",
    "branch_return",
    "terminal_branch",
}


@dataclass
class Port:
    """设备端口"""
    device_id: str
    port_index: int  # 从 0 开始
    direction: Literal["supply", "return", "bidirectional"]  # supply=出, return=入
    medium: Medium | None
    diameter_mm: float | None = None
    floor_id: str | None = None
    is_valid: bool = True
    owner_kind: Literal["device", "pipe"] = "device"


@dataclass
class Edge:
    """连接边"""
    from_port: Port
    to_port: Port
    medium: Medium
    pipe_id: str | None = None  # 如果通过管道连接
    length_m: float = 0.0
    diameter_mm: float | None = None


@dataclass(frozen=True)
class ManifoldPortBinding:
    """A stable connection-to-port assignment for one manifold endpoint."""

    manifold_id: str
    peer_id: str
    port_index: int
    medium: Medium
    kind: Literal["main", "branch"]


@dataclass
class PortGraph:
    """端口级拓扑图"""
    ports: list[Port]
    edges: list[Edge]
    device_ports: dict[str, list[Port]]  # device_id -> ports
    adjacency: dict[tuple[str, int], list[Edge]]  # (device_id, port_index) -> edges
    medium_subgraphs: dict[Medium, "PortGraph"]  # 按介质分离的子图
    manifold_bindings: list[ManifoldPortBinding]


def build_port_graph(request: SystemEvaluateRequest) -> PortGraph:
    """构建端口级拓扑图"""
    ports: list[Port] = []
    device_ports: dict[str, list[Port]] = defaultdict(list)
    manifold_layouts = {
        device.id: _build_manifold_layout(device.id, request)
        for device in request.devices
        if _is_manifold_device(device)
    }

    # 1. 枚举设备端口
    for device in sorted(request.devices, key=lambda item: item.id):
        device_port_list = _enumerate_device_ports(
            device,
            request,
            manifold_layouts.get(device.id),
        )
        ports.extend(device_port_list)
        device_ports[device.id] = device_port_list

    # 物理管段也是端口图节点：两端通过一条携带阻力参数的真实边连接。
    referenced_endpoint_ids = {
        endpoint_id
        for connection in request.connections
        for endpoint_id in (connection.from_id, connection.to_id)
    }
    for pipe in sorted(request.pipes, key=lambda item: item.id):
        if pipe.id not in referenced_endpoint_ids:
            continue
        pipe_ports = [
            Port(
                pipe.id,
                0,
                "bidirectional",
                pipe.system,
                pipe.diameter_mm,
                pipe.floor_id,
                True,
                "pipe",
            ),
            Port(
                pipe.id,
                1,
                "bidirectional",
                pipe.system,
                pipe.diameter_mm,
                pipe.floor_id,
                True,
                "pipe",
            ),
        ]
        ports.extend(pipe_ports)
        device_ports[pipe.id] = pipe_ports

    # 2. 构建连接边
    edges, manifold_bindings = _build_edges(
        request,
        device_ports,
        manifold_layouts,
    )

    # 3. 构建邻接表
    adjacency: dict[tuple[str, int], list[Edge]] = defaultdict(list)
    for edge in edges:
        key_from = (edge.from_port.device_id, edge.from_port.port_index)
        key_to = (edge.to_port.device_id, edge.to_port.port_index)
        adjacency[key_from].append(edge)
        adjacency[key_to].append(edge)

    # 4. 按介质分离子图
    medium_subgraphs = _split_by_medium(ports, edges, manifold_bindings)

    return PortGraph(
        ports=ports,
        edges=edges,
        device_ports=device_ports,
        adjacency=adjacency,
        medium_subgraphs=medium_subgraphs,
        manifold_bindings=manifold_bindings,
    )


@dataclass(frozen=True)
class PortLayoutEntry:
    """目录级端口布局条目。

    项目端口枚举与 /api/v1/devices/ports 查询共用的单一事实来源。
    conditional=True 的口（如 FCU/AHU 热水口）在项目里要接了对应系统
    才参与编号；目录查询不做筛选，原样以 optional 暴露给前端空置。
    """

    direction: Literal["supply", "return", "bidirectional"]
    medium: Medium | None
    conditional: bool = False


def _role_port_entries(role: str) -> list[PortLayoutEntry]:
    """角色 → 端口布局表（单一事实来源）。"""
    if role == "chiller":
        # 冷冻水供回 + 冷却水供回
        return [
            PortLayoutEntry("return", "chilled_water"),
            PortLayoutEntry("supply", "chilled_water"),
            PortLayoutEntry("supply", "cooling_water"),
            PortLayoutEntry("return", "cooling_water"),
        ]
    if role == "pump":
        # 暂不区分冷冻/冷却水，介质待连接推断
        return [
            PortLayoutEntry("return", None),
            PortLayoutEntry("supply", None),
        ]
    if role == "cooling_tower":
        return [
            PortLayoutEntry("return", "cooling_water"),
            PortLayoutEntry("supply", "cooling_water"),
        ]
    if role == "fcu" or role == "ahu":
        # 水侧供回（热水口为条件性）+ 风侧送回
        return [
            PortLayoutEntry("return", "chilled_water"),
            PortLayoutEntry("supply", "chilled_water"),
            PortLayoutEntry("return", "hot_water", conditional=True),
            PortLayoutEntry("supply", "hot_water", conditional=True),
            PortLayoutEntry("supply", "air"),
            PortLayoutEntry("return", "air"),
        ]
    if role == "fresh_air_unit":
        # 新风进口 / 送风出口
        return [
            PortLayoutEntry("return", "air"),
            PortLayoutEntry("supply", "air"),
        ]
    if role == "water_valve" or role == "air_valve":
        return [
            PortLayoutEntry("bidirectional", None),
            PortLayoutEntry("bidirectional", None),
        ]
    if role == "manifold":
        # 仅主口；分支口由连接布局动态决定（见 _build_manifold_layout）
        return [PortLayoutEntry("bidirectional", None)]
    if "pipe" in role:
        return [
            PortLayoutEntry("bidirectional", None),
            PortLayoutEntry("bidirectional", None),
        ]
    # 其他角色，默认2个双向端口
    return [
        PortLayoutEntry("bidirectional", None),
        PortLayoutEntry("bidirectional", None),
    ]


def _heuristic_port_entries(
    device_type: str, catalog_role: str | None
) -> tuple[str, list[PortLayoutEntry]] | None:
    """无目录规格时按类型名推断端口布局；识别不了返回 None。"""
    if catalog_role == "heat_source":
        return "heat_source", [
            PortLayoutEntry("return", "hot_water"),
            PortLayoutEntry("supply", "hot_water"),
        ]
    if "冷" in device_type and "机组" in device_type:
        # 冷机类设备：冷冻水 + 冷却水各2个端口
        return "chiller", [
            PortLayoutEntry("return", "chilled_water"),
            PortLayoutEntry("supply", "chilled_water"),
            PortLayoutEntry("supply", "cooling_water"),
            PortLayoutEntry("return", "cooling_water"),
        ]
    if "泵" in device_type:
        # 泵类设备：2个端口（介质待推断）
        return "pump", [
            PortLayoutEntry("return", None),
            PortLayoutEntry("supply", None),
        ]
    if "风机盘管" in device_type:
        # FCU：水侧2 + 风侧2（与角色表不同，不带条件性热水口）
        return "fcu", [
            PortLayoutEntry("return", "chilled_water"),
            PortLayoutEntry("supply", "chilled_water"),
            PortLayoutEntry("supply", "air"),
            PortLayoutEntry("return", "air"),
        ]
    return None


def _role_value(role: object) -> str:
    """DeviceRole 枚举转字符串；str() 会得到 'DeviceRole.CHILLER'，必须取 .value。"""
    return role.value if isinstance(role, DeviceRole) else str(role)


@dataclass(frozen=True)
class DevicePortLayout:
    """/api/v1/devices/ports 的目录级端口布局查询结果。"""

    role: str
    source: Literal["catalog", "type_default", "inferred"]
    dynamic_ports: bool
    entries: list[PortLayoutEntry]
    model_name: str | None = None


def query_device_port_layout(
    device_type: str, model_name: str | None = None
) -> DevicePortLayout | None:
    """按设备类型（可选型号）查询静态端口布局。

    型号验证以文字参数库（与 GET /api/v1/catalog/equipment 同源）为准，
    精确/别名命中则 source=catalog 并回显规范型号名；角色按类型解析
    （参数库 role 只到 cold_source/terminal 粒度，区分不了 FCU/AHU/新风）。
    条件性端口与动态分支口只做标记，不在此猜测连接。
    无法识别返回 None（接口层转 404）。
    """
    device = DeviceInput(id="catalog_query", type=device_type, name=model_name)
    model_spec = _text_catalog_model_hit(device)
    model_hit_name = str(model_spec.get("name")) if model_spec else None

    unity_role = get_device_role(device_type)
    if unity_role is not None:
        role = _role_value(unity_role)
        return DevicePortLayout(
            role=role,
            source="catalog" if model_spec else "type_default",
            dynamic_ports=_is_manifold_layout(role, device_type),
            entries=_role_port_entries(role),
            model_name=model_hit_name,
        )
    heuristic = _heuristic_port_entries(device_type, role_for_device(device))
    if heuristic is not None:
        role, entries = heuristic
        return DevicePortLayout(
            role=role,
            source="catalog" if model_spec else "inferred",
            dynamic_ports=_is_manifold_layout(role, device_type),
            entries=entries,
            model_name=model_hit_name,
        )
    return None


def _text_catalog_model_hit(device: DeviceInput) -> dict[str, Any] | None:
    """文字参数库 (type, name) 精确/别名命中；通配 "*" 默认条目不算命中。"""
    if not device.name:
        return None
    spec = spec_for_device(device)
    if spec is None or str(spec.get("name")) == "*":
        return None
    names = {str(spec.get("name"))} | {str(alias) for alias in spec.get("aliases", [])}
    return spec if str(device.name) in names else None


def _is_manifold_layout(role: str, device_type: str) -> bool:
    return role == DeviceRole.MANIFOLD.value or device_type in MANIFOLD_DEVICE_TYPES


def _enumerate_device_ports(
    device: DeviceInput,
    request: SystemEvaluateRequest,
    manifold_layout: dict[tuple[Medium, str], int] | None = None,
) -> list[Port]:
    """枚举设备端口

    基于 unity_catalog 角色判断端口数量和方向：
    - 冷机/热泵：冷冻水供回（2）+ 冷却水供回（2）或热水供回（2）
    - 泵：进出各1（2）
    - 冷却塔：进出各1（2）
    - FCU/AHU：水侧供回（2）+ 风侧送回（2）
    - 新风机组：风侧送回（2）
    - 管件/阀件/异径：两端（2）
    - 分集水器：1个主口 + N个分支口
    """
    spec = _find_unity_spec(device)
    catalog_role = role_for_device(device)

    if manifold_layout is not None:
        port_media: dict[int, set[Medium]] = defaultdict(set)
        for (medium, _peer_id), port_index in manifold_layout.items():
            port_media[port_index].add(medium)

        branch_count = max(manifold_layout.values(), default=0)
        return [
            Port(
                device.id,
                port_index,
                "bidirectional",
                next(iter(port_media[port_index]))
                if len(port_media[port_index]) == 1
                else None,
                None,
                device.floor_id,
                True,
            )
            for port_index in range(branch_count + 1)
        ]

    # 布局表见 _role_port_entries / _heuristic_port_entries（与目录查询共用）
    if spec is not None and spec.role:
        entries = _role_port_entries(spec.role)
    else:
        heuristic = _heuristic_port_entries(device.type, catalog_role)
        entries = (
            heuristic[1]
            if heuristic is not None
            else [
                # 未知设备，假设2个双向端口
                PortLayoutEntry("bidirectional", None),
                PortLayoutEntry("bidirectional", None),
            ]
        )
    # 条件性端口（FCU/AHU 热水口）只在本设备接了热水系统时参与编号
    uses_hot_water = _device_uses_system(request, device.id, "hot_water")
    active_entries = [
        entry for entry in entries if not entry.conditional or uses_hot_water
    ]
    return [
        Port(
            device.id,
            index,
            entry.direction,
            entry.medium,
            None,
            device.floor_id,
            True,
        )
        for index, entry in enumerate(active_entries)
    ]


def _find_unity_spec(device: DeviceInput):
    """Resolve Unity port metadata for formal catalog models and legacy names."""
    spec = find_equipment_spec(device.type, device.name or "")
    if spec is not None:
        return spec

    catalog_spec = spec_for_device(device)
    if catalog_spec is None:
        return None
    for alias in catalog_spec.get("aliases", []):
        spec = find_equipment_spec(device.type, str(alias))
        if spec is not None:
            return spec
    return None


def _device_uses_system(request: SystemEvaluateRequest, device_id: str, system: Medium) -> bool:
    return any(
        connection.system == system
        and device_id in {connection.from_id, connection.to_id}
        for connection in request.connections
    )


def _is_manifold_device(device: DeviceInput) -> bool:
    unity_role = get_device_role(device.type)
    return unity_role == DeviceRole.MANIFOLD or device.type in MANIFOLD_DEVICE_TYPES


def _build_manifold_layout(
    manifold_id: str,
    request: SystemEvaluateRequest,
) -> dict[tuple[Medium, str], int]:
    """Assign port 0 to mains and stable ports 1..N to branch peers."""
    pipes = {pipe.id: pipe for pipe in request.pipes}
    devices = {device.id: device for device in request.devices}
    link_kinds: dict[Medium, dict[str, set[str]]] = defaultdict(
        lambda: defaultdict(set)
    )

    for connection in request.connections:
        if manifold_id not in {connection.from_id, connection.to_id}:
            continue
        peer_id = (
            connection.to_id
            if connection.from_id == manifold_id
            else connection.from_id
        )
        link_kinds[connection.system][peer_id].add(
            _classify_manifold_peer(peer_id, pipes, devices)
        )

    resolved: dict[tuple[Medium, str], Literal["main", "branch"]] = {}
    for medium in sorted(link_kinds):
        peer_kinds = link_kinds[medium]
        explicit_main = sorted(
            peer_id for peer_id, kinds in peer_kinds.items() if "main" in kinds
        )
        explicit_branch = sorted(
            peer_id
            for peer_id, kinds in peer_kinds.items()
            if "main" not in kinds and "branch" in kinds
        )
        unknown = sorted(
            peer_id
            for peer_id, kinds in peer_kinds.items()
            if kinds == {"unknown"}
        )

        for peer_id in explicit_main:
            resolved[(medium, peer_id)] = "main"
        for peer_id in explicit_branch:
            resolved[(medium, peer_id)] = "branch"

        if explicit_main:
            for peer_id in unknown:
                resolved[(medium, peer_id)] = "branch"
        elif unknown:
            resolved[(medium, unknown[0])] = "main"
            for peer_id in unknown[1:]:
                resolved[(medium, peer_id)] = "branch"

    branch_links = sorted(
        link
        for link, kind in resolved.items()
        if kind == "branch"
    )
    branch_ports = {
        link: port_index
        for port_index, link in enumerate(branch_links, start=1)
    }
    return {
        link: 0 if kind == "main" else branch_ports[link]
        for link, kind in resolved.items()
    }


def _classify_manifold_peer(
    peer_id: str,
    pipes: dict[str, PipeInput],
    devices: dict[str, DeviceInput],
) -> Literal["main", "branch", "unknown"]:
    pipe = pipes.get(peer_id)
    if pipe is not None:
        if pipe.pipe_type in MANIFOLD_MAIN_PIPE_TYPES:
            return "main"
        if pipe.pipe_type in MANIFOLD_BRANCH_PIPE_TYPES:
            return "branch"
        return "unknown"

    device = devices.get(peer_id)
    if device is None:
        return "unknown"

    unity_role = get_device_role(device.type)
    role = unity_role.value if unity_role else role_for_device(device)
    if role in {"fcu", "ahu", "fresh_air_unit", "terminal"}:
        return "branch"
    if role in {
        "chiller",
        "cold_source",
        "heat_source",
        "pump",
        "cooling_tower",
    }:
        return "main"
    return "unknown"


def _build_edges(
    request: SystemEvaluateRequest,
    device_ports: dict[str, list[Port]],
    manifold_layouts: dict[str, dict[tuple[Medium, str], int]],
) -> tuple[list[Edge], list[ManifoldPortBinding]]:
    """构建连接边

    基于 ConnectionInput 推断端口匹配：
    - from_id/to_id 可能是设备或管道
    - 设备连接：匹配介质和方向（supply -> return）
    - 管道连接：管道作为中间节点
    """
    edges: list[Edge] = []
    bindings: list[ManifoldPortBinding] = []
    pipe_ids = {pipe.id for pipe in request.pipes}
    pipe_endpoint_use: dict[str, int] = defaultdict(int)

    # 管段物理属性只出现一次；ConnectionInput 只负责连接端点。
    for pipe in sorted(request.pipes, key=lambda item: item.id):
        if pipe.id not in device_ports:
            continue
        pipe_ports = device_ports[pipe.id]
        edges.append(
            Edge(
                pipe_ports[0],
                pipe_ports[1],
                pipe.system,
                pipe.id,
                pipe.length_m,
                pipe.diameter_mm,
            )
        )

    for conn in sorted(
        request.connections,
        key=lambda item: (item.system, item.from_id, item.to_id),
    ):
        medium = conn.system
        from_id, to_id = conn.from_id, conn.to_id

        # 设备和管段统一按端点连接；管段端口按连接顺序稳定分配。
        if from_id in device_ports and to_id in device_ports:
            from_port = _find_endpoint_port(
                from_id,
                device_ports[from_id],
                medium,
                "supply",
                pipe_ids,
                pipe_endpoint_use,
                manifold_layouts,
                to_id,
            )
            to_port = _find_endpoint_port(
                to_id,
                device_ports[to_id],
                medium,
                "return",
                pipe_ids,
                pipe_endpoint_use,
                manifold_layouts,
                from_id,
            )

            if from_port and to_port:
                edges.append(Edge(from_port, to_port, medium, None, 0.0, None))
                _record_manifold_binding(
                    bindings,
                    manifold_layouts,
                    from_id,
                    to_id,
                    medium,
                    from_port,
                )
                _record_manifold_binding(
                    bindings,
                    manifold_layouts,
                    to_id,
                    from_id,
                    medium,
                    to_port,
                )
            else:
                # 尝试反向匹配或双向匹配
                from_port_any = _find_matching_port(device_ports[from_id], medium, None)
                to_port_any = _find_matching_port(device_ports[to_id], medium, None)
                if from_port_any and to_port_any:
                    edges.append(Edge(from_port_any, to_port_any, medium, None, 0.0, None))

    return edges, bindings


def _record_manifold_binding(
    bindings: list[ManifoldPortBinding],
    manifold_layouts: dict[str, dict[tuple[Medium, str], int]],
    endpoint_id: str,
    peer_id: str,
    medium: Medium,
    port: Port,
) -> None:
    if endpoint_id not in manifold_layouts:
        return
    bindings.append(
        ManifoldPortBinding(
            manifold_id=endpoint_id,
            peer_id=peer_id,
            port_index=port.port_index,
            medium=medium,
            kind="main" if port.port_index == 0 else "branch",
        )
    )


def _find_endpoint_port(
    endpoint_id: str,
    ports: list[Port],
    medium: Medium,
    direction: str,
    pipe_ids: set[str],
    pipe_endpoint_use: dict[str, int],
    manifold_layouts: dict[str, dict[tuple[Medium, str], int]],
    peer_id: str,
) -> Port | None:
    manifold_layout = manifold_layouts.get(endpoint_id)
    if manifold_layout is not None:
        port_index = manifold_layout.get((medium, peer_id))
        if port_index is None:
            return None
        return next(
            (port for port in ports if port.port_index == port_index),
            None,
        )

    if endpoint_id not in pipe_ids:
        return _find_matching_port(ports, medium, direction)

    matching = [port for port in ports if port.medium == medium]
    if not matching:
        return None
    index = min(pipe_endpoint_use[endpoint_id], len(matching) - 1)
    pipe_endpoint_use[endpoint_id] += 1
    return matching[index]


def _find_matching_port(ports: list[Port], medium: Medium, direction: str | None) -> Port | None:
    """查找匹配介质和方向的端口

    Args:
        ports: 端口列表
        medium: 目标介质
        direction: 目标方向（"supply"/"return"/None 表示任意）
    """
    for port in ports:
        # 检查介质匹配
        if port.medium is not None and port.medium != medium:
            continue
        if port.medium is None:
            # 端口介质未定义，可以匹配任意介质
            pass

        # 检查方向匹配
        if direction is None:
            # 不限方向，只要介质匹配即可
            return port
        elif port.direction == direction or port.direction == "bidirectional":
            return port

    return None


def _split_by_medium(
    ports: list[Port],
    edges: list[Edge],
    manifold_bindings: list[ManifoldPortBinding],
) -> dict[Medium, PortGraph]:
    """按介质分离子图

    只包含该介质的边，以及这些边连接的端口（不包含孤立的 medium=None 端口）

    注意：邻接表中，边作为无向边处理（双向可达），且同一设备的同介质端口视为内部连通
    """
    subgraphs: dict[Medium, PortGraph] = {}

    for medium in ("chilled_water", "cooling_water", "hot_water", "air"):
        # 只选择该介质的边
        medium_edges = [e for e in edges if e.medium == medium]

        # 收集这些边涉及的端口
        port_keys: set[tuple[str, int]] = set()
        for edge in medium_edges:
            port_keys.add((edge.from_port.device_id, edge.from_port.port_index))
            port_keys.add((edge.to_port.device_id, edge.to_port.port_index))

        # 只包含边涉及的端口
        medium_ports = [p for p in ports if (p.device_id, p.port_index) in port_keys]

        if medium_ports or medium_edges:
            # 构建子图邻接表（边作为无向边处理）
            adjacency: dict[tuple[str, int], list[Edge]] = defaultdict(list)
            for edge in medium_edges:
                key_from = (edge.from_port.device_id, edge.from_port.port_index)
                key_to = (edge.to_port.device_id, edge.to_port.port_index)
                adjacency[key_from].append(edge)
                adjacency[key_to].append(edge)

            # 为连通性检查添加设备内部连接（虚拟边）
            # 同一设备的同介质端口之间应该是连通的
            device_ports_sub: dict[str, list[Port]] = defaultdict(list)
            for port in medium_ports:
                device_ports_sub[port.device_id].append(port)

            physical_pipe_ids = {edge.pipe_id for edge in medium_edges if edge.pipe_id}

            # 为每个设备的端口对创建虚拟内部边。物理管段已有真实内部边，不能短接。
            internal_edges: list[Edge] = []
            for device_id, device_port_list in device_ports_sub.items():
                if device_id in physical_pipe_ids:
                    continue
                # 对于同一设备的端口，两两之间添加虚拟边（用于连通性检查）
                for i, port_a in enumerate(device_port_list):
                    for port_b in device_port_list[i+1:]:
                        # 创建虚拟边（长度为0，表示设备内部）
                        internal_edge = Edge(
                            from_port=port_a,
                            to_port=port_b,
                            medium=medium,
                            length_m=0.0,
                            diameter_mm=None,
                        )
                        internal_edges.append(internal_edge)

                        key_a = (port_a.device_id, port_a.port_index)
                        key_b = (port_b.device_id, port_b.port_index)
                        adjacency[key_a].append(internal_edge)
                        adjacency[key_b].append(internal_edge)

            subgraphs[medium] = PortGraph(
                ports=medium_ports,
                edges=medium_edges,  # 只包含真实的外部边，不包含虚拟内部边
                device_ports=device_ports_sub,
                adjacency=adjacency,  # 包含虚拟内部边，用于连通性检查
                medium_subgraphs={},  # 子图不再递归分离
                manifold_bindings=[
                    binding
                    for binding in manifold_bindings
                    if binding.medium == medium
                ],
            )

    return subgraphs
