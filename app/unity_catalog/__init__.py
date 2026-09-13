"""Unity 设备目录映射模块

覆盖 Unity 27 类 427 型号的设备目录映射，定义设备角色与参数映射。
"""

from .catalog import find_equipment_spec, get_all_device_specs, get_device_spec
from .roles import DeviceRole, get_device_role

__all__ = [
    "DeviceRole",
    "find_equipment_spec",
    "get_all_device_specs",
    "get_device_role",
    "get_device_spec",
]
