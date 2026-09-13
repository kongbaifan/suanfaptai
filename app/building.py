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


def room_floor_id(room) -> str:
    return room.floor_id or DEFAULT_FLOOR_ID


def device_floor_id(device) -> str:
    return device.floor_id or DEFAULT_FLOOR_ID


def pipe_floor_id(pipe: PipeInput) -> str:
    return pipe.floor_id or pipe.from_floor_id or pipe.to_floor_id or DEFAULT_FLOOR_ID


def node_floor_id(request: SystemEvaluateRequest, node_id: str) -> str:
    for device in request.devices:
        if device.id == node_id:
            return device_floor_id(device)
    for pipe in request.pipes:
        if pipe.id == node_id:
            return pipe_floor_id(pipe)
    return DEFAULT_FLOOR_ID


def pipe_floor_span(pipe: PipeInput) -> tuple[str, str]:
    start = pipe.from_floor_id or pipe.floor_id or DEFAULT_FLOOR_ID
    end = pipe.to_floor_id or pipe.floor_id or start
    return start, end


def is_riser(pipe: PipeInput) -> bool:
    return pipe.pipe_type in RISER_TYPES


def served_floor_ids(request: SystemEvaluateRequest, pipe: PipeInput) -> list[str]:
    floors = normalized_floors(request)
    by_id = {floor.id: floor for floor in floors}
    start, end = pipe_floor_span(pipe)
    if start not in by_id or end not in by_id:
        return [pipe_floor_id(pipe)]
    start_level = by_id[start].level
    end_level = by_id[end].level
    low, high = sorted((start_level, end_level))
    served = [floor.id for floor in floors if low <= floor.level <= high]
    return served or [start]


def floor_area_map(request: SystemEvaluateRequest) -> dict[str, float]:
    areas: dict[str, float] = defaultdict(float)
    for floor in normalized_floors(request):
        areas[floor.id] += 0.0
    for room in request.rooms:
        areas[room_floor_id(room)] += room.area_m2
    return dict(areas)


def floor_base_summary(request: SystemEvaluateRequest) -> dict[str, dict[str, Any]]:
    floors = floor_by_id(request)
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
        floor_id = room_floor_id(room)
        item = summary.setdefault(floor_id, _unknown_floor_summary(floor_id))
        item["area_m2"] += room.area_m2
        item["people"] += room.people
        item["room_count"] += 1
    for device in request.devices:
        floor_id = device_floor_id(device)
        item = summary.setdefault(floor_id, _unknown_floor_summary(floor_id))
        item["device_count"] += device.quantity
    for pipe in request.pipes:
        floor_id = pipe_floor_id(pipe)
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
