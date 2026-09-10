"""Unity 设备目录映射模块

覆盖 Unity 27 类 427 型号的设备目录映射，定义设备角色与参数映射。
"""

from .catalog import get_device_spec, get_all_device_specs, find_equipment_spec
from .roles import DeviceRole, get_device_role

__all__ = [
    "get_device_spec",
    "get_all_device_specs",
    "find_equipment_spec",
    "DeviceRole",
    "get_device_role",
]
