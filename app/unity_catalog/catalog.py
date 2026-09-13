"""Unity 设备目录

从 DeviceLibrary.json 加载 427 条设备映射，提供 (DeviceID, ModelID) 查询。
"""

import json
from dataclasses import dataclass
from typing import Literal

from app.release import resource_path

from .geometry import (
    parse_copper_pipe_od,
    parse_dn,
    parse_rect_size,
    parse_reducer_pair,
)
from .roles import DeviceRole, get_device_role, get_solver_behaviors, get_system_type

# 参数来源类型
ParameterSource = Literal["measured", "engineering_default", "derived_from_name", "unresolved"]


@dataclass
class GeometricParams:
    """几何参数（从 DeviceName 派生）"""
    dn: float | None = None  # 管道 DN（mm）
    width: float | None = None  # 矩形宽度（mm）
    height: float | None = None  # 矩形高度（mm）
    od: float | None = None  # 铜管外径（mm）
    reducer_large: float | None = None  # 异径管大端（mm）
    reducer_small: float | None = None  # 异径管小端（mm）


@dataclass
class DeviceSpec:
    """设备规格"""
    # Unity 稳定键
    device_id: int
    model_id: int

    # Unity 标识字段
    device_type: str  # Unity DeviceType（如"冷源机组"）
    device_name: str  # Unity DeviceName（已归一为 str）
    device_library: str  # Unity DeviceLibrary（如"暖通设备"）

    # 角色与语义
    role: DeviceRole | None  # 设备角色
    system_type: str | None  # 支持系统（water/air/both）
    solver_behaviors: list[str]  # 求解行为列表

    # 几何参数（派生）
    geometric: GeometricParams

    # 参数来源与质量
    source: ParameterSource
    issues: list[str]  # 质量问题列表


# 全局目录缓存
_CATALOG: list[DeviceSpec] | None = None
_CATALOG_INDEX: dict[tuple[int, int], DeviceSpec] | None = None


def _load_device_library() -> list[dict]:
    """加载 DeviceLibrary.json"""
    json_path = resource_path(
        "Assets/StreamingAssets/SoftwareSerializationData/Json/DeviceLibrary.json"
    )

    if not json_path.exists():
        raise FileNotFoundError(f"DeviceLibrary.json not found at {json_path}")

    with open(json_path, encoding="utf-8") as f:
        return json.load(f)


def _parse_geometric_params(device_type: str, device_name: str) -> GeometricParams:
    """根据设备类型和名称派生几何参数"""
    geom = GeometricParams()

    # 管道类：DN 或矩形尺寸
    if device_type in ["圆直管", "定位圆管", "分水器", "集水器"]:
        geom.dn = parse_dn(device_name)
    elif device_type in ["定位方管"]:
        size = parse_rect_size(device_name)
        if size:
            geom.width, geom.height = size
    elif device_type == "定位铜管":
        geom.od = parse_copper_pipe_od(device_name)
    elif device_type == "铜异径管":
        pair = parse_reducer_pair(device_name)
        if pair:
            geom.reducer_large, geom.reducer_small = pair
    elif device_type in ["圆三通", "圆四通"]:
        geom.dn = parse_dn(device_name)
    elif device_type in ["方三通", "方四通"]:
        size = parse_rect_size(device_name)
        if size:
            geom.width, geom.height = size

    return geom


def _build_catalog() -> tuple[list[DeviceSpec], dict[tuple[int, int], DeviceSpec]]:
    """构建设备目录和索引"""
    raw_data = _load_device_library()
    catalog: list[DeviceSpec] = []
    index: dict[tuple[int, int], DeviceSpec] = {}

    for entry in raw_data:
        device_id = entry["DeviceID"]
        model_id = entry["ModelID"]
        device_type = entry["DeviceType"]
        # 归一化 DeviceName 为 str（铜管为数字字面量）
        device_name = str(entry["DeviceName"])
        device_library = entry["DeviceLibrary"]

        # 角色解析
        role = get_device_role(device_type)
        system_type = get_system_type(role).value if role else None
        solver_behaviors = [b.value for b in get_solver_behaviors(role)] if role else []

        # 几何参数派生
        geometric = _parse_geometric_params(device_type, device_name)

        # 参数来源与质量
        issues = []
        source: ParameterSource = "derived_from_name"

        # 容量类参数标记为 unresolved
        if role in [DeviceRole.CHILLER, DeviceRole.PUMP, DeviceRole.COOLING_TOWER,
                    DeviceRole.FCU, DeviceRole.AHU, DeviceRole.FRESH_AIR_UNIT]:
            issues.append(f"Capacity/COP/performance parameters unresolved for {device_type}")
            source = "unresolved"

        # 泵角色需拓扑判定
        if role == DeviceRole.PUMP:
            issues.append("Pump role (chilled/cooling water) requires topology traversal")

        spec = DeviceSpec(
            device_id=device_id,
            model_id=model_id,
            device_type=device_type,
            device_name=device_name,
            device_library=device_library,
            role=role,
            system_type=system_type,
            solver_behaviors=solver_behaviors,
            geometric=geometric,
            source=source,
            issues=issues,
        )

        catalog.append(spec)
        index[(device_id, model_id)] = spec

    return catalog, index


def get_device_spec(device_id: int, model_id: int) -> DeviceSpec | None:
    """根据 (DeviceID, ModelID) 获取设备规格

    Args:
        device_id: Unity DeviceID
        model_id: Unity ModelID

    Returns:
        设备规格，未找到返回 None
    """
    global _CATALOG, _CATALOG_INDEX

    if _CATALOG is None or _CATALOG_INDEX is None:
        _CATALOG, _CATALOG_INDEX = _build_catalog()

    return _CATALOG_INDEX.get((device_id, model_id))


def get_all_device_specs() -> list[DeviceSpec]:
    """获取所有设备规格（427 条）"""
    global _CATALOG, _CATALOG_INDEX

    if _CATALOG is None or _CATALOG_INDEX is None:
        _CATALOG, _CATALOG_INDEX = _build_catalog()

    return _CATALOG


def find_equipment_spec(device_type: str, device_name: str) -> DeviceSpec | None:
    """根据 (type, name) 查找设备规格

    提供向后兼容的查询接口。

    Args:
        device_type: Unity DeviceType
        device_name: Unity DeviceName（已归一为 str）

    Returns:
        设备规格，未找到返回 None
    """
    if _CATALOG is None:
        get_all_device_specs()  # 触发加载（赋值在那里完成，此处只读）

    device_name_str = str(device_name)  # 确保字符串比较

    for spec in _CATALOG:  # type: ignore
        if spec.device_type == device_type and spec.device_name == device_name_str:
            return spec

    return None
