"""新风与末端能力校验

补充 `validate_system` 未覆盖、但会让仿真静默失真的两类配置问题：

1. **新风处理设备不具备制冷能力**：房间的新风量会被判给"新风承担者"
   （``_fresh_air_handlers``），房间侧随即不再单独注入新风。若该承担者本身
   没有可用冷源（例如目录里的"一体式新风机组"是直膨机，而仿真暂无直膨模型，
   且样例也没接冷冻水），新风负荷就会两头落空、凭空消失。此规则把这种配置
   显式暴露出来。
2. **末端选型严重超配**：末端总名义冷量远超房间设计负荷时，阀门开到极小
   开度房间就已过冷，控制失去调节余地。教学项目应提示重新选型。
"""

from __future__ import annotations

from collections import defaultdict

from app.calculators.load import calculate_load
from app.data.design_defaults import PURPOSE_DEFAULTS
from app.room_bindings import resolve_room_bindings
from app.schemas import Issue, LoadRequest, SystemEvaluateRequest
from app.simcore.rated import resolve_rated_spec_by_type
from app.unity_catalog import DeviceRole

# 具备引入并处理室外新风能力的角色
_FRESH_AIR_ROLES = {DeviceRole.AHU, DeviceRole.FRESH_AIR_UNIT}
# 仿真中没有任何制冷通路的末端类型（无水路、且无直膨模型）
_DX_FAU_TYPES = {"一体式新风机组"}
_WATER_SYSTEMS = {"chilled_water", "hot_water"}
# 末端总名义冷量 / 房间设计冷负荷 超过该倍率即提示超配
_OVERSIZE_RATIO = 2.5


def validate_fresh_air_and_terminals(
    request: SystemEvaluateRequest,
) -> list[Issue]:
    if not request.rooms or not request.devices:
        return []

    issues: list[Issue] = []
    room_by_id = {room.id: room for room in request.rooms}
    device_by_id = {device.id: device for device in request.devices}

    # 设备接入了哪些水系统
    water_systems_by_device: dict[str, set[str]] = defaultdict(set)
    for connection in request.connections:
        if connection.system in _WATER_SYSTEMS:
            for device_id in (connection.from_id, connection.to_id):
                water_systems_by_device[device_id].add(connection.system)

    # 房间设计冷负荷（与仿真同源）
    load_by_room: dict[str, float] = {}
    try:
        load_result = calculate_load(
            LoadRequest(
                rooms=request.rooms,
                floors=request.floors,
                simultaneity_factor=request.simultaneity_factor,
                safety_factor=request.safety_factor,
            )
        )
        load_by_room = {item.id: item.cooling_kw for item in load_result.rooms}
    except Exception:  # noqa: BLE001 - 设计负荷算不出来时跳过超配规则
        load_by_room = {}

    # 每个房间可用的新风引入设备
    bindings = resolve_room_bindings(request).room_ids_by_device
    fresh_air_capable_by_room: dict[str, list[str]] = defaultdict(list)
    for device in request.devices:
        role = resolve_rated_spec_by_type(device.type, device.name).role
        if role not in _FRESH_AIR_ROLES:
            continue
        for room_id in bindings.get(device.id, ()):
            fresh_air_capable_by_room[room_id].append(device.id)

    for room in request.rooms:
        if (room.people or 0) <= 0:
            continue
        # 该房间按人数算出的新风量（与 load.py 同口径，仅用于判断"是否需要"）
        defaults_fresh_air = _fresh_air_per_person(room.purpose)
        if defaults_fresh_air <= 0:
            continue
        capable = sorted(fresh_air_capable_by_room.get(room.id, []))
        if not capable:
            issues.append(
                Issue(
                    level="warning",
                    code="fresh_air_no_handler",
                    message=(
                        f"房间 {room.name or room.id} 有新风需求，但没有任何"
                        "组合式空调机组/新风机组承担，新风将直接进入房间。"
                    ),
                    target=room.id,
                )
            )
            continue
        # 有承担者，但承担者是否真的能处理新风？
        functional = [
            device_id
            for device_id in capable
            if water_systems_by_device.get(device_id)
            or device_by_id[device_id].type not in _DX_FAU_TYPES
        ]
        if not functional:
            issues.append(
                Issue(
                    level="warning",
                    code="fresh_air_handler_without_cooling",
                    message=(
                        f"房间 {room.name or room.id} 的新风由"
                        f" {', '.join(capable)} 承担，但该设备没有可用冷源"
                        "（未接冷冻水且仿真无直膨模型），新风湿负荷将被丢弃。"
                        "请改用接冷冻水的组合式空调机组。"
                    ),
                    target=room.id,
                )
            )

    issues.extend(_oversize_issues(request, load_by_room, room_by_id))
    return issues


def _fresh_air_per_person(purpose: str | None) -> float:
    """按用途查每人新风量；查不到按常规办公取值。"""
    key = (purpose or "").strip().lower()
    defaults = PURPOSE_DEFAULTS.get(key)
    if defaults is None:
        defaults = PURPOSE_DEFAULTS.get("office", {})
    return float(defaults.get("fresh_air_m3_h_person", 0.0))


def _oversize_issues(
    request: SystemEvaluateRequest,
    load_by_room: dict[str, float],
    room_by_id: dict[str, object],
) -> list[Issue]:
    """末端名义冷量相对房间设计负荷是否严重超配。

    只统计**房间制冷末端**，并做两处折算，避免误报：

    - 承担新风处理的机组（组合式/新风机组）容量主要用来处理室外新风，不算
      房间显热末端，否则一台机组会把每个共服房间都算成超配；
    - 多房间共用的末端按**服务房间数均分**，而不是把整机容量记到每个房间。
    """
    if not load_by_room:
        return []
    binding = resolve_room_bindings(request).room_ids_by_device
    fresh_air_handlers = set(_fresh_air_handler_ids(request, binding))
    capacity_by_room: dict[str, float] = defaultdict(float)
    for device in request.devices:
        if device.id in fresh_air_handlers:
            continue
        served_rooms = binding.get(device.id, ())
        if not served_rooms:
            continue
        rated = resolve_rated_spec_by_type(device.type, device.name).capacity_kw
        if rated is None:
            continue
        share = rated * device.quantity / len(served_rooms)
        for room_id in served_rooms:
            capacity_by_room[room_id] += share

    issues: list[Issue] = []
    for room_id, design_kw in sorted(load_by_room.items()):
        capacity_kw = capacity_by_room.get(room_id, 0.0)
        if capacity_kw <= 0.0 or design_kw <= 0.0:
            continue
        ratio = capacity_kw / design_kw
        if ratio <= _OVERSIZE_RATIO:
            continue
        room = room_by_id.get(room_id)
        name = getattr(room, "name", None) or room_id
        issues.append(
            Issue(
                level="warning",
                code="terminal_oversized",
                message=(
                    f"房间 {name} 房间末端名义冷量约 {capacity_kw:.1f} kW，"
                    f"约为设计负荷 {design_kw:.1f} kW 的 {ratio:.1f} 倍，"
                    "阀门小开度即过冷，建议重新选型。"
                ),
                target=room_id,
            )
        )
    return issues


def _fresh_air_handler_ids(
    request: SystemEvaluateRequest,
    binding,
) -> set[str]:
    """房间新风承担者候选：组合式空调机组与新风机组。

    它们的容量主要用来处理室外新风，不计入房间显热末端的超配评估。
    """
    return {
        device.id
        for device in request.devices
        if resolve_rated_spec_by_type(device.type, device.name).role
        in {DeviceRole.AHU, DeviceRole.FRESH_AIR_UNIT}
        and binding.get(device.id)
    }

