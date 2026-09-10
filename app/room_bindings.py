"""Resolve room-to-terminal service relationships at the project boundary.

绑定的**权威方向是房间 → 设备**：`RoomInput.device_ids` 列出服务该房间的末端。
房间是建筑实体，设备装在房间里，所以清单跟着房间走——删房间即删清单，
不需要反过来遍历所有设备做级联清理。

仿真核内部需要的是反方向（一台末端把冷量摊到它服务的若干房间），
所以本模块的职责就是在边界上把房间侧清单**倒置**成 `room_ids_by_device`。
倒置结果按房间在列表中的出现顺序排列，与房间数组顺序无关的部分保持稳定。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from app.schemas import Issue, RoomInput, SystemEvaluateRequest
from app.simcore.rated import resolve_rated_spec_by_type
from app.unity_catalog import DeviceRole


TERMINAL_ROLES = frozenset(
    {DeviceRole.FCU, DeviceRole.AHU, DeviceRole.FRESH_AIR_UNIT}
)


@dataclass(frozen=True, slots=True)
class RoomBindingResolution:
    room_ids_by_device: Mapping[str, tuple[str, ...]]
    issues: tuple[Issue, ...]

    @property
    def errors(self) -> tuple[Issue, ...]:
        return tuple(issue for issue in self.issues if issue.level == "error")


class ProjectRoomBindingError(ValueError):
    def __init__(self, issues: tuple[Issue, ...]) -> None:
        self.issues = issues
        super().__init__("Project room bindings are invalid.")


def room_instance_id(room: RoomInput, index: int) -> str:
    return room.id or f"room-{index + 1}"


def resolve_room_bindings(
    project: SystemEvaluateRequest,
    *,
    roles_by_device: Mapping[str, DeviceRole] | None = None,
) -> RoomBindingResolution:
    """Invert `RoomInput.device_ids` into per-device room tuples."""
    roles = {
        device.id: (
            roles_by_device[device.id]
            if roles_by_device is not None and device.id in roles_by_device
            else resolve_rated_spec_by_type(device.type, device.name).role
        )
        for device in project.devices
    }
    terminal_ids = {
        device_id for device_id, role in roles.items() if role in TERMINAL_ROLES
    }

    bindings: dict[str, list[str]] = {device.id: [] for device in project.devices}
    issues: list[Issue] = []

    for index, room in enumerate(project.rooms):
        room_id = room_instance_id(room, index)
        seen: set[str] = set()
        duplicates: set[str] = set()
        for device_id in room.device_ids:
            if device_id in seen:
                duplicates.add(device_id)
            seen.add(device_id)
        for device_id in sorted(duplicates):
            issues.append(
                _error(
                    "ROOM_BINDING_DUPLICATE",
                    f"Room {room_id} lists device {device_id} more than once.",
                    room_id,
                )
            )

        for device_id in dict.fromkeys(room.device_ids):
            if device_id not in bindings:
                issues.append(
                    _error(
                        "ROOM_BINDING_UNKNOWN",
                        f"Room {room_id} references unknown device {device_id}.",
                        room_id,
                    )
                )
                continue
            if device_id not in terminal_ids:
                issues.append(
                    _error(
                        "ROOM_BINDING_UNSUPPORTED_DEVICE",
                        f"Device {device_id} is not a terminal and cannot serve room {room_id}.",
                        room_id,
                    )
                )
                continue
            bindings[device_id].append(room_id)

        if not any(device_id in terminal_ids for device_id in room.device_ids):
            # 这条警告正是旧「按数组位置取第一个房间」实现静默吞掉的情况：
            # 房间参与了负荷计算，却没有任何末端为它供冷。
            issues.append(
                _warning(
                    "ROOM_BINDING_NO_DEVICE",
                    f"Room {room_id} has no terminal device serving it.",
                    room_id,
                )
            )

    for device_id in sorted(terminal_ids):
        if not bindings[device_id]:
            issues.append(
                _warning(
                    "ROOM_BINDING_DEVICE_UNCLAIMED",
                    f"Terminal {device_id} is not listed by any room.",
                    device_id,
                )
            )

    return RoomBindingResolution(
        room_ids_by_device={
            device_id: tuple(room_ids) for device_id, room_ids in bindings.items()
        },
        issues=tuple(issues),
    )


def raise_for_room_binding_errors(resolution: RoomBindingResolution) -> None:
    if resolution.errors:
        raise ProjectRoomBindingError(resolution.errors)


def _error(code: str, message: str, target: str) -> Issue:
    return Issue(level="error", code=code, message=message, target=target)


def _warning(code: str, message: str, target: str) -> Issue:
    return Issue(level="warning", code=code, message=message, target=target)
