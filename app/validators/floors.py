from collections import defaultdict

from app.building import (
    RISER_TYPES,
    floor_ids,
    implicit_floor_id,
    is_riser,
    pipe_floor_span,
)
from app.data.equipment import role_for_device
from app.schemas import Issue, SystemEvaluateRequest


def validate_floors(request: SystemEvaluateRequest) -> list[Issue]:
    """楼层引用校验。

    **只校验显式声明的楼层**（`tests/test_floor_fallback.py` 双向钉死）。
    未声明楼层的节点由 `building.implicit_floor_id` 兜底到项目里真实存在的
    第一个楼层，不在这里判——此前的写法先兜底成字面量 `default_floor` 再比对，
    于是"项目声明了 F1/F2、节点没写楼层"会凭空报 error：两个 manifold 样例
    的 18 条 `UNKNOWN_PIPE_FLOOR`（error）全部是这么来的，而前端 `addPipe()`
    建管路时本来就不写楼层，等于默认操作就会变红。

    显式写错楼层的仍照旧报 error——降噪不能把真错一起消掉。
    """
    issues: list[Issue] = []
    known_floor_ids = floor_ids(request)

    for room in request.rooms:
        floor_id = room.floor_id
        if floor_id and floor_id not in known_floor_ids:
            issues.append(Issue(level="error", code="UNKNOWN_ROOM_FLOOR", message=f"Room {room.name} references unknown floor {floor_id}.", target=room.id or room.name))

    for device in request.devices:
        floor_id = device.floor_id
        if floor_id and floor_id not in known_floor_ids:
            issues.append(Issue(level="error", code="UNKNOWN_DEVICE_FLOOR", message=f"Device {device.id} references unknown floor {floor_id}.", target=device.id))

    for pipe in request.pipes:
        declared_floors = [
            floor_id
            for floor_id in (pipe.floor_id, pipe.from_floor_id, pipe.to_floor_id)
            if floor_id
        ]
        for floor_id in declared_floors:
            if floor_id not in known_floor_ids:
                issues.append(Issue(level="error", code="UNKNOWN_PIPE_FLOOR", message=f"Pipe {pipe.id} references unknown floor {floor_id}.", target=pipe.id))

        start_floor, end_floor = pipe_floor_span(
            pipe, implicit=implicit_floor_id(request)
        )
        crosses_floor = start_floor != end_floor
        if crosses_floor and not is_riser(pipe):
            issues.append(Issue(level="warning", code="INVALID_CROSS_FLOOR_PIPE", message=f"Pipe {pipe.id} crosses floors but pipe_type is {pipe.pipe_type}; use riser_supply/riser_return for vertical pipes.", target=pipe.id))
        if pipe.pipe_type in RISER_TYPES and (not pipe.from_floor_id or not pipe.to_floor_id):
            issues.append(Issue(level="warning", code="RISER_FLOOR_SPAN_MISSING", message=f"Riser {pipe.id} should provide from_floor_id and to_floor_id.", target=pipe.id))

    issues.extend(_pipe_type_connection_issues(request))
    return _dedupe_issues(issues)


def _pipe_type_connection_issues(request: SystemEvaluateRequest) -> list[Issue]:
    issues: list[Issue] = []
    pipe_by_id = {pipe.id: pipe for pipe in request.pipes}
    device_by_id = {device.id: device for device in request.devices}
    terminal_counts: dict[str, int] = defaultdict(int)

    for connection in request.connections:
        pipe = pipe_by_id.get(connection.from_id) or pipe_by_id.get(connection.to_id)
        if not pipe:
            continue
        other_id = connection.to_id if connection.from_id == pipe.id else connection.from_id
        other_device = device_by_id.get(other_id)
        other_role = role_for_device(other_device) if other_device else None
        if pipe.pipe_type in {"main_supply", "main_return"} and other_role == "terminal":
            issues.append(Issue(level="warning", code="MAIN_PIPE_CONNECTED_TO_TERMINAL", message=f"Main pipe {pipe.id} is directly connected to terminal {other_id}; use branch or terminal_branch.", target=pipe.id))
        if pipe.pipe_type == "terminal_branch" and other_role == "terminal":
            terminal_counts[pipe.id] += 1

    for pipe_id, count in terminal_counts.items():
        if count > 1:
            issues.append(Issue(level="warning", code="TERMINAL_BRANCH_MULTI_TERMINAL", message=f"Terminal branch {pipe_id} connects {count} terminals; split branches for clearer balancing.", target=pipe_id))
    return issues


def _dedupe_issues(issues: list[Issue]) -> list[Issue]:
    deduped: list[Issue] = []
    seen = set()
    for issue in issues:
        key = (issue.level, issue.code, issue.message, issue.target)
        if key not in seen:
            seen.add(key)
            deduped.append(issue)
    return deduped
