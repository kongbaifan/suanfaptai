"""系统制式校验：按声明的空调系统形式检查拓扑是否成立

`validate_system` / `validate_topology` 都是**制式无关**的：它们只问"这个系统
自洽吗"（有没有冷源、环路闭合吗、介质有没有混接），从不问"这是哪一种空调系统
形式"。于是两族差异极大的系统形式在代码里退化成同一个"设备 + 管路 + 连接的
集合"：学生把一台风机盘管混进全空气系统，或者"风机盘管 + 新风"系统压根没配
新风处理设备，都不会得到任何反馈。本模块补上这一层。

设计口径（三条，都会影响可测行为）：

1. **制式未声明时返回空列表**。存量项目与全部内置样例都未声明制式，其校验
   输出必须与引入本模块之前逐字节一致。"未声明制式"这件事由制式自检入口
   （``/api/v1/system/form-check``）用 info 级 issue 显式告知，不污染通用校验。
2. **规则比对的是"制式要求"与"拓扑现状"**，每条都指出整改方向，而不是只说
   "配置错误"。
3. **制式规则接管通用校验里被弱化的同名判断**。``fresh_air_no_handler``
   （``validators/fresh_air.py``）是 warning 且只按房间报；声明制式后由本模块
   的制式级 error 接管，组合点用 ``superseded_issue_codes`` 把它过滤掉，
   避免同一件事报两遍。
"""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Mapping

from app.data.equipment import role_for_device
from app.room_bindings import resolve_room_bindings, room_instance_id
from app.schemas import (
    Issue,
    SystemEvaluateRequest,
    SystemForm,
    SystemFormCheckResult,
)
from app.simcore.rated import resolve_rated_spec_by_type
from app.unity_catalog import DeviceRole

# 与 `validators/fresh_air.py` 同一口径：仿真里没有直膨模型，靠盘管处理空气的
# 机组必须接入水系统才有冷热量产出。取冷冻水与热水两者，避免采暖工况误报。
_WATER_SYSTEMS = frozenset({"chilled_water", "hot_water"})

# 各制式的**房间末端**角色。房间 `device_ids` 必须命中它，否则没有设备给该房间供冷。
_ROOM_TERMINAL_ROLE: dict[SystemForm, DeviceRole] = {
    SystemForm.FCU_FRESH_AIR: DeviceRole.FCU,
    SystemForm.ALL_AIR: DeviceRole.AHU,
}

# 各制式的**新风承担**角色。这是两族最本质的区别，也是"温湿分控"的定义特征：
#   风机盘管 + 新风：新风必须由**独立新风机组**处理，风机盘管只处理房间回风；
#   全空气：新风由组合式机组的混风箱承担，与回风混合后过同一副盘管。
_FRESH_AIR_HANDLER_ROLE: dict[SystemForm, DeviceRole] = {
    SystemForm.FCU_FRESH_AIR: DeviceRole.FRESH_AIR_UNIT,
    SystemForm.ALL_AIR: DeviceRole.AHU,
}

# 制式不允许出现的末端族。
#   全空气制式下的风机盘管：属于另一族的房间末端，混装即不再是全空气系统。
#   全空气制式下的独立新风机组：会**抢走**组合式机组的新风职责——
#     `session._fresh_air_handlers` 里新风机组优先级（0）高于组合式机组（1），
#     同一房间的新风只算给一台设备，于是组合式机组拿不到新风量，
#     混风箱退化成全回风，项目实际变成混合制式。
_FORBIDDEN_TERMINAL_ROLES: dict[SystemForm, tuple[DeviceRole, ...]] = {
    SystemForm.FCU_FRESH_AIR: (DeviceRole.AHU,),
    SystemForm.ALL_AIR: (DeviceRole.FCU, DeviceRole.FRESH_AIR_UNIT),
}

# 角色 → 中文名，用于把整改方向写成人话。
_ROLE_LABELS: dict[DeviceRole, str] = {
    DeviceRole.FCU: "风机盘管",
    DeviceRole.AHU: "组合式空调机组",
    DeviceRole.FRESH_AIR_UNIT: "新风机组",
}

# 声明制式后由本模块接管、应从通用校验里剔除的 issue 码。
_SUPERSEDED_CODES = frozenset({"fresh_air_no_handler"})

# 风口类型名（目录里"送风口"与"回风口"同属 DeviceRole.AIR_OUTLET，
# 只能靠类型名区分，见 CLAUDE.md 的风侧设备语义一节）。
# 空气系统的"风路终点"角色，与 `calculators/network_paths.py::_path_endpoints`
# 对 air 系统的口径保持一致：风口（air_terminal）与风阀（air_accessory）。
_AIR_PATH_ENDPOINT_ROLES = frozenset({"air_terminal", "air_accessory"})


def superseded_issue_codes(form: SystemForm | None) -> frozenset[str]:
    """声明制式后应被制式规则取代的通用 issue 码。

    未声明制式时返回空集合，通用校验原样保留。
    """
    return _SUPERSEDED_CODES if form is not None else frozenset()


def check_system_form(request: SystemEvaluateRequest) -> SystemFormCheckResult:
    """制式自检：只回答"声明的系统制式在这份拓扑上成立吗"。

    与 `validate_system_form` 的区别只有一处：**未声明制式时这里会显式报出
    一条 info**，而通用校验路径下必须完全静默（见模块 docstring 的口径 1）。
    自检是学生主动发问的入口，"你没声明制式，所以这些条件都没检查"正是他
    需要知道的信息；而把这个 info 塞进每次评估的 issue 列表就只是噪音。
    """
    issues = validate_system_form(request)
    if request.system_form is None:
        issues = [
            Issue(
                level="info",
                code="SYSTEM_FORM_UNDECLARED",
                message=(
                    "项目未声明空调系统制式，本次只做与制式无关的通用校验。"
                    "声明为「风机盘管 + 新风」或「全空气」后，才会检查房间"
                    "末端族、新风由谁承担、以及全空气的风管是否接成一次循环。"
                ),
            ),
            *issues,
        ]
    return SystemFormCheckResult(
        system_form=request.system_form,
        declared=request.system_form is not None,
        compliant=not any(issue.level == "error" for issue in issues),
        issues=issues,
    )


def validate_system_form(
    request: SystemEvaluateRequest,
    *,
    roles_by_device: Mapping[str, DeviceRole] | None = None,
) -> list[Issue]:
    """按 `request.system_form` 校验拓扑是否满足该制式的成立条件。

    `roles_by_device` 允许调用方复用已经解析好的角色表（``resolve_rated_spec_by_type``
    每次调用都要读一遍设备目录），不传则在本函数内解析一次。
    """
    form = request.system_form
    if form is None:
        # 未声明制式：本模块完全不参与，通用校验的输出保持不变。
        return []
    if not request.devices:
        # "一台设备都没有"由通用校验的 NO_DEVICES / MISSING_TERMINAL 报，
        # 这里再报一遍是噪音。
        return []

    roles = (
        dict(roles_by_device)
        if roles_by_device is not None
        else roles_by_device_of(request)
    )
    # `room_ids_by_device` 是**设备 → 房间**的倒置表（房间侧清单才是权威方向，
    # 见 `room_bindings.py` 的模块说明）。本模块要按房间查末端，故再倒置一次。
    room_ids_by_device = resolve_room_bindings(
        request, roles_by_device=roles
    ).room_ids_by_device
    devices_by_room: dict[str, list[str]] = defaultdict(list)
    for device_id, room_ids in room_ids_by_device.items():
        for room_id in room_ids:
            devices_by_room[room_id].append(device_id)

    water_connected = _water_connected_device_ids(request)
    air_connected = _air_connected_device_ids(request)

    ids_by_role: dict[DeviceRole, list[str]] = defaultdict(list)
    for device in request.devices:
        role = roles.get(device.id)
        if role is not None:
            ids_by_role[role].append(device.id)

    required_role = _ROOM_TERMINAL_ROLE[form]
    handler_role = _FRESH_AIR_HANDLER_ROLE[form]
    required_label = _ROLE_LABELS[required_role]
    handler_label = _ROLE_LABELS[handler_role]

    issues: list[Issue] = []
    issues.extend(_forbidden_terminal_issues(form, roles, request))
    issues.extend(
        _terminal_family_issues(
            request,
            form=form,
            devices_by_room=devices_by_room,
            water_connected=water_connected,
            required_role=required_role,
            required_label=required_label,
            ids_by_role=ids_by_role,
        )
    )
    issues.extend(
        _fresh_air_issues(
            request,
            form=form,
            devices_by_room=devices_by_room,
            water_connected=water_connected,
            air_connected=air_connected,
            handler_role=handler_role,
            handler_label=handler_label,
            ids_by_role=ids_by_role,
        )
    )
    return issues


def roles_by_device_of(
    request: SystemEvaluateRequest,
) -> dict[str, DeviceRole]:
    """解析每台设备的角色。

    `resolve_rated_spec_by_type` 没有缓存，每调用一次都会重新读取并解析设备
    目录，因此这里**只解析一遍**再复用，避免按设备数重复读盘。
    """
    return {
        device.id: resolve_rated_spec_by_type(device.type, device.name).role
        for device in request.devices
    }


def _forbidden_terminal_issues(
    form: SystemForm,
    roles: Mapping[str, DeviceRole],
    request: SystemEvaluateRequest,
) -> list[Issue]:
    """制式不允许出现的末端族。"""
    forbidden = _FORBIDDEN_TERMINAL_ROLES[form]
    found: dict[DeviceRole, list[str]] = defaultdict(list)
    for device in request.devices:
        role = roles.get(device.id)
        if role in forbidden:
            found[role].append(device.id)

    issues: list[Issue] = []
    for role in forbidden:
        device_ids = sorted(found.get(role, ()))
        if not device_ids:
            continue
        issues.append(
            Issue(
                level="error",
                code="SYSTEM_FORM_FORBIDDEN_TERMINAL",
                message=(
                    f"本项目的制式是「{_form_label(form)}」，其中不应出现"
                    f"{_ROLE_LABELS[role]}（{', '.join(device_ids)}）。"
                    + _forbidden_reason(form, role)
                ),
                target=device_ids[0],
            )
        )
    return issues


def _forbidden_reason(form: SystemForm, role: DeviceRole) -> str:
    """把"为什么不允许"讲清楚，学生才知道该删设备还是该改制式。"""
    if form is SystemForm.ALL_AIR and role is DeviceRole.FCU:
        return (
            "全空气系统用组合式机组集中处理空气、再由风管送到各房间；"
            "风机盘管是分散在各房间的另一族末端，混装后房间的冷量来源"
            "不再唯一，风量分配与室温控制都会失去可比性。"
            "若要保留风机盘管，应把制式改为「风机盘管 + 新风」。"
        )
    if form is SystemForm.ALL_AIR and role is DeviceRole.FRESH_AIR_UNIT:
        return (
            "全空气系统的新风由组合式机组的混风箱承担（新风与回风混合后"
            "过同一副盘管）。独立新风机组会按更高的优先级接管房间新风，"
            "组合式机组因此拿不到新风量，混风箱退化成全回风，项目实际上"
            "变成了混合制式。请二选一：删掉新风机组，或把制式改为"
            "「风机盘管 + 新风」。"
        )
    if form is SystemForm.FCU_FRESH_AIR and role is DeviceRole.AHU:
        return (
            "「风机盘管 + 新风」的房间空气由风机盘管就地循环、新风单独处理；"
            "组合式机组一次处理整层乃至整栋的空气，是另一族（全空气）的做法。"
            "请二选一：删掉组合式机组，或把制式改为「全空气」。"
        )
    return ""


def _terminal_family_issues(
    request: SystemEvaluateRequest,
    *,
    form: SystemForm,
    devices_by_room: Mapping[str, list[str]],
    water_connected: set[str],
    required_role: DeviceRole,
    required_label: str,
    ids_by_role: Mapping[DeviceRole, list[str]],
) -> list[Issue]:
    """房间末端族：有没有、覆盖了哪些房间、接没接水、有没有接错风管。"""
    issues: list[Issue] = []
    terminals = sorted(ids_by_role.get(required_role, ()))
    form_label = _form_label(form)

    if not terminals:
        issues.append(
            Issue(
                level="error",
                code="SYSTEM_FORM_TERMINAL_MISSING",
                message=(
                    f"制式「{form_label}」的房间末端是{required_label}，"
                    f"但项目里一台都没有——房间没有任何设备能消除室内得热。"
                    "请先添加末端，或在评估前把制式改为与实际设备一致的形式。"
                ),
            )
        )
        # 没有末端族时，"哪台末端没接水""哪个房间没末端"都无从谈起。
        return issues

    # 每台末端都要接水：没有水侧就没有冷热量产出（仿真无直膨模型）。
    dry = [device_id for device_id in terminals if device_id not in water_connected]
    for device_id in sorted(dry):
        issues.append(
            Issue(
                level="error",
                code="SYSTEM_FORM_TERMINAL_NOT_WATER_CONNECTED",
                message=(
                    f"{required_label} {device_id} 没有接入水系统"
                    "（冷冻水/热水），仿真中不产出冷热量，房间负荷将无人承担。"
                    "请把它接进冷冻水环路。"
                ),
                target=device_id,
            )
        )

    # 每个房间都要有本制式的末端服务。
    terminal_set = set(terminals)
    for index, room in enumerate(request.rooms):
        room_id = room_instance_id(room, index)
        served = [
            device_id
            for device_id in devices_by_room.get(room_id, ())
            if device_id in terminal_set
        ]
        if served:
            continue
        issues.append(
            Issue(
                level="error",
                code="SYSTEM_FORM_ROOM_TERMINAL_MISSING",
                message=(
                    f"房间 {room.name or room_id} 没有{required_label}服务。"
                    f"制式「{form_label}」要求每个房间都有自己的房间末端，"
                    "请在房间的末端清单里补上。"
                ),
                target=room_id,
            )
        )

    # 全空气制式还要检查风管是否真的接成了一次循环。
    if form is SystemForm.ALL_AIR:
        issues.extend(_ahu_duct_issues(request, terminals))

    return issues


def _ahu_duct_issues(
    request: SystemEvaluateRequest, ahu_ids: list[str]
) -> list[Issue]:
    """全空气制式：组合式机组空气侧的送风与回风是否都接出去了。

    这里必须用**端口图**判断，不能用"节点连不连通"：组合式机组的风侧送、
    回是两个方向相反的口，只有端口图知道哪条边落在哪个口上。节点级连通性
    会把送回风两侧并成一张图，一台只接送风管、回风侧悬空的机组也能"连通"。
    """
    # 延迟导入：`app.steady_solver/__init__.py` 会 import solver，而 solver
    # 经 calculators 反过来 import validators，顶层导入会形成包级循环。
    from app.steady_solver.port_graph import build_port_graph

    graph = build_port_graph(request)
    issues: list[Issue] = []
    for device_id in sorted(ahu_ids):
        ports = graph.device_ports.get(device_id, [])
        air_ports = [port for port in ports if port.medium == "air"]
        missing: list[str] = []
        for direction, label in (("supply", "送风"), ("return", "回风")):
            port = next(
                (item for item in air_ports if item.direction == direction), None
            )
            if port is None or not graph.adjacency.get((device_id, port.port_index)):
                missing.append(label)
        if missing:
            issues.append(
                Issue(
                    level="error",
                    code="SYSTEM_FORM_AHU_DUCT_INCOMPLETE",
                    message=(
                        f"组合式空调机组 {device_id} 的空气侧缺少"
                        f"{ '、'.join(missing) }管路。全空气系统靠风管把处理好的"
                        "空气送到房间、再把房间空气抽回来，送回两侧缺一侧机组"
                        "就无法完成一次循环。请补上风管连接。"
                    ),
                    target=device_id,
                )
            )
            continue

        reachable = _air_reachable_device_ids(request, device_id)
        # "风路终点"沿用项目自己的定义（`calculators/network_paths.py` 的
        # `_path_endpoints`）：空气系统的终点是角色为 `air_terminal` 或
        # `air_accessory` 的设备，即风口与风阀。只认"回风口"会把以风阀收尾的
        # 回风路径误判成断路——`multi_zone_all_air` 样例正是这样接的。
        if not any(
            role_for_device(device) in _AIR_PATH_ENDPOINT_ROLES
            for device in request.devices
            if device.id in reachable
        ):
            issues.append(
                Issue(
                    level="warning",
                    code="SYSTEM_FORM_AHU_OUTLET_MISSING",
                    message=(
                        f"组合式空调机组 {device_id} 的风路上没有任何风口或风阀。"
                        "风管接出去了，但没有房间侧的送/回风口把空气送进、抽出"
                        "房间，全空气系统实际上没有与房间连通。"
                    ),
                    target=device_id,
                )
            )
    return issues


def _fresh_air_issues(
    request: SystemEvaluateRequest,
    *,
    form: SystemForm,
    devices_by_room: Mapping[str, list[str]],
    water_connected: set[str],
    air_connected: set[str],
    handler_role: DeviceRole,
    handler_label: str,
    ids_by_role: Mapping[DeviceRole, list[str]],
) -> list[Issue]:
    """新风承担设备：有没有、覆盖了哪些房间、接没接水、能不能送出风。"""
    issues: list[Issue] = []
    handlers = sorted(ids_by_role.get(handler_role, ()))
    form_label = _form_label(form)

    if not handlers:
        issues.append(
            Issue(
                level="error",
                code="SYSTEM_FORM_FRESH_AIR_HANDLER_MISSING",
                message=(
                    f"制式「{form_label}」要求新风由{handler_label}集中处理，"
                    f"但项目里没有{handler_label}。此时房间按人数算出的新风会"
                    "按室外状态直接进入房间，新风的显热与湿负荷没有任何设备"
                    "承担，房间温度与湿度都会算不达标。请补上处理新风的机组。"
                ),
            )
        )
        return issues

    handler_set = set(handlers)
    for device_id in sorted(handlers):
        if device_id not in water_connected:
            issues.append(
                Issue(
                    level="error",
                    code="SYSTEM_FORM_FRESH_AIR_HANDLER_NOT_WATER_CONNECTED",
                    message=(
                        f"{handler_label} {device_id} 承担房间新风，但没有接入"
                        "水系统（冷冻水/热水），仿真中不产出冷热量，新风的"
                        "显热与湿负荷会被丢弃。请给它接上冷冻水。"
                    ),
                    target=device_id,
                )
            )
        if device_id not in air_connected:
            issues.append(
                Issue(
                    level="warning",
                    code="SYSTEM_FORM_FRESH_AIR_UNIT_NO_AIR_PATH",
                    message=(
                        f"{handler_label} {device_id} 没有接出风管，处理后的"
                        "新风送不到房间（机组的风侧端口悬空）。请把送风管路"
                        "接上。"
                    ),
                    target=device_id,
                )
            )

    # 每个有新风需求的房间，都要由本制式的新风承担设备覆盖。
    for index, room in enumerate(request.rooms):
        room_id = room_instance_id(room, index)
        if (room.people or 0) <= 0:
            # 没有人就没有卫生新风需求，不要求配新风设备。
            continue
        served = [
            device_id
            for device_id in devices_by_room.get(room_id, ())
            if device_id in handler_set
        ]
        if served:
            continue
        issues.append(
            Issue(
                level="error",
                code="SYSTEM_FORM_ROOM_FRESH_AIR_UNCOVERED",
                message=(
                    f"房间 {room.name or room_id} 有新风需求（{room.people} 人），"
                    f"但没有{handler_label}为它承担新风。该房间的新风会按室外"
                    "状态直接进入房间，湿负荷无处承担。请把这台机组也列进该"
                    "房间的末端清单。"
                ),
                target=room_id,
            )
        )
    return issues


def _water_connected_device_ids(request: SystemEvaluateRequest) -> set[str]:
    """出现在水系统连接上的设备/管路节点 id。"""
    connected: set[str] = set()
    for connection in request.connections:
        if connection.system not in _WATER_SYSTEMS:
            continue
        connected.add(connection.from_id)
        connected.add(connection.to_id)
    return connected


def _air_connected_device_ids(request: SystemEvaluateRequest) -> set[str]:
    """出现在空气系统连接上的设备/管路节点 id。"""
    return {
        endpoint
        for connection in request.connections
        if connection.system == "air"
        for endpoint in (connection.from_id, connection.to_id)
    }


def _air_reachable_device_ids(
    request: SystemEvaluateRequest, start_id: str
) -> set[str]:
    """空气系统里从 `start_id` 出发（忽略方向）能到达的全部节点 id。"""
    adjacency: dict[str, set[str]] = defaultdict(set)
    for connection in request.connections:
        if connection.system != "air":
            continue
        adjacency[connection.from_id].add(connection.to_id)
        adjacency[connection.to_id].add(connection.from_id)

    seen = {start_id}
    queue: deque[str] = deque([start_id])
    while queue:
        node = queue.popleft()
        for peer in adjacency.get(node, ()):
            if peer not in seen:
                seen.add(peer)
                queue.append(peer)
    return seen


def _form_label(form: SystemForm) -> str:
    return {
        SystemForm.FCU_FRESH_AIR: "风机盘管 + 新风",
        SystemForm.ALL_AIR: "全空气",
    }[form]
