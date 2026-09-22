from collections import defaultdict
from typing import Any, Protocol

from app.schemas import FloorInput, PipeInput, SystemEvaluateRequest

DEFAULT_FLOOR_ID = "default_floor"
RISER_TYPES = {"riser_supply", "riser_return"}
SUPPLY_TYPES = {"main_supply", "branch_supply", "terminal_branch", "riser_supply"}
RETURN_TYPES = {"main_return", "branch_return", "riser_return"}


class HasFloors(Protocol):
    """只声明楼层的请求。LoadRequest 与 SystemEvaluateRequest 都满足，两者不共享基类。"""

    floors: list[FloorInput]


def normalized_floors(request: HasFloors) -> list[FloorInput]:
    if request.floors:
        return sorted(request.floors, key=lambda floor: (floor.level, floor.elevation_m, floor.id))
    return [FloorInput(id=DEFAULT_FLOOR_ID, name="默认楼层", level=0, elevation_m=0.0, height_m=3.6)]


def floor_ids(request: SystemEvaluateRequest) -> set[str]:
    return {floor.id for floor in normalized_floors(request)}


def floor_by_id(request: SystemEvaluateRequest) -> dict[str, FloorInput]:
    return {floor.id: floor for floor in normalized_floors(request)}


def implicit_floor_id(request: HasFloors) -> str:
    """节点**没有声明楼层**时归属哪个楼层。

    取项目里**真实存在的第一个楼层**（`normalized_floors` 已按标高排序；项目
    一台楼层都没声明时它会合成一个 `DEFAULT_FLOOR_ID` 的楼层，故此时返回的
    正是那个 id）。

    历史缺陷：`room_floor_id` / `device_floor_id` / `pipe_floor_id` 原先直接
    兜底成**字面量** `DEFAULT_FLOOR_ID`。项目没声明楼层时这是对的，但项目
    **声明了楼层**（如 `B1F/F1/F2`）时，这个 id 根本不存在，于是每个没写
    楼层的节点都被 `validators/floors.py` 判成 `UNKNOWN_*_FLOOR` **error**：

    - `MANIFOLD_FCU_SAMPLE` 与 `MANIFOLD_FCU_DUAL_WATER_SAMPLE` 的 18 根管子
      全部省略 `floor_id`，两个样例的 error 级 issue 因此**全部**是假错；
    - 前端 `addPipe()` 建管路时也不设 `floor_id`，"新增管路"在默认路径上就变红。

    `implicit` 参数只用于兜底：**显式声明**了楼层的节点不受影响，写错的楼层
    仍会被照实报出（见 `tests/test_floor_fallback.py`）。
    """
    floors = normalized_floors(request)
    return floors[0].id if floors else DEFAULT_FLOOR_ID


def room_floor_id(room, *, implicit: str = DEFAULT_FLOOR_ID) -> str:
    return room.floor_id or implicit


def device_floor_id(device, *, implicit: str = DEFAULT_FLOOR_ID) -> str:
    return device.floor_id or implicit


def pipe_floor_id(pipe: PipeInput, *, implicit: str = DEFAULT_FLOOR_ID) -> str:
    return pipe.floor_id or pipe.from_floor_id or pipe.to_floor_id or implicit


def node_floor_id(request: SystemEvaluateRequest, node_id: str) -> str:
    implicit = implicit_floor_id(request)
    for device in request.devices:
        if device.id == node_id:
            return device_floor_id(device, implicit=implicit)
    for pipe in request.pipes:
        if pipe.id == node_id:
            return pipe_floor_id(pipe, implicit=implicit)
    return implicit


def pipe_floor_span(
    pipe: PipeInput, *, implicit: str = DEFAULT_FLOOR_ID
) -> tuple[str, str]:
    start = pipe.from_floor_id or pipe.floor_id or implicit
    end = pipe.to_floor_id or pipe.floor_id or start
    return start, end


def is_riser(pipe: PipeInput) -> bool:
    return pipe.pipe_type in RISER_TYPES


def served_floor_ids(request: SystemEvaluateRequest, pipe: PipeInput) -> list[str]:
    floors = normalized_floors(request)
    by_id = {floor.id: floor for floor in floors}
    start, end = pipe_floor_span(pipe, implicit=implicit_floor_id(request))
    if start not in by_id or end not in by_id:
        return [pipe_floor_id(pipe, implicit=implicit_floor_id(request))]
    start_level = by_id[start].level
    end_level = by_id[end].level
    low, high = sorted((start_level, end_level))
    served = [floor.id for floor in floors if low <= floor.level <= high]
    return served or [start]


def floor_area_map(request: SystemEvaluateRequest) -> dict[str, float]:
    areas: dict[str, float] = defaultdict(float)
    implicit = implicit_floor_id(request)
    for floor in normalized_floors(request):
        areas[floor.id] += 0.0
    for room in request.rooms:
        areas[room_floor_id(room, implicit=implicit)] += room.area_m2
    return dict(areas)


def floor_base_summary(request: SystemEvaluateRequest) -> dict[str, dict[str, Any]]:
    floors = floor_by_id(request)
    implicit = implicit_floor_id(request)
    summary: dict[str, dict[str, Any]] = {
        floor.id: {
            "floor_id": floor.id,
            "floor_name": floor.name or floor.id,
            "level": floor.level,
            "area_m2": 0.0,
            "people": 0,
            "room_count": 0,
            "device_count": 0,
            "pipe_count": 0,
            "riser_count": 0,
        }
        for floor in floors.values()
    }
    for room in request.rooms:
        floor_id = room_floor_id(room, implicit=implicit)
        item = summary.setdefault(floor_id, _unknown_floor_summary(floor_id))
        item["area_m2"] += room.area_m2
        item["people"] += room.people
        item["room_count"] += 1
    for device in request.devices:
        floor_id = device_floor_id(device, implicit=implicit)
        item = summary.setdefault(floor_id, _unknown_floor_summary(floor_id))
        item["device_count"] += device.quantity
    for pipe in request.pipes:
        floor_id = pipe_floor_id(pipe, implicit=implicit)
        item = summary.setdefault(floor_id, _unknown_floor_summary(floor_id))
        item["pipe_count"] += 1
        if is_riser(pipe):
            item["riser_count"] += 1
    for item in summary.values():
        item["area_m2"] = round(float(item["area_m2"]), 3)
    return summary


def floor_weight_map(request: SystemEvaluateRequest) -> dict[str, float]:
    areas = floor_area_map(request)
    floors = [floor.id for floor in normalized_floors(request)]
    total = sum(areas.values())
    if total <= 0:
        return {floor_id: round(1.0 / max(len(floors), 1), 6) for floor_id in floors}
    return {floor_id: areas.get(floor_id, 0.0) / total for floor_id in floors}


def _unknown_floor_summary(floor_id: str) -> dict[str, Any]:
    return {
        "floor_id": floor_id,
        "floor_name": floor_id,
        "level": 0,
        "area_m2": 0.0,
        "people": 0,
        "room_count": 0,
        "device_count": 0,
        "pipe_count": 0,
        "riser_count": 0,
    }
