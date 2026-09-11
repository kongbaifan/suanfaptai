"""历史设备类型名的向后兼容

目录里 `一体式新风机组` 改名为 `新风机组` 后，改名之前保存的项目仍带旧类型名。
若不做归一，这些项目会静默退化：角色解析不到（端口布局错乱）、额定参数掉进
内置兜底（冷量/水量与实际机型无关）。这里锁定三个解析入口都能识别旧名。
"""

from copy import deepcopy

from app.data.equipment import find_equipment_spec, resolve_device_type
from app.samples import FOUR_FLOOR_FCU_FRESH_AIR_SAMPLE
from app.schemas import SystemEvaluateRequest
from app.simcore.project import build_project_layout
from app.simcore.rated import resolve_rated_spec_by_type
from app.unity_catalog import DeviceRole, get_device_role


def test_legacy_type_name_normalizes_to_current():
    assert resolve_device_type("一体式新风机组") == "新风机组"
    # 现名与未知类型原样返回
    assert resolve_device_type("新风机组") == "新风机组"
    assert resolve_device_type("不存在类型") == "不存在类型"
    assert resolve_device_type(None) == ""


def test_legacy_type_resolves_role_and_catalog_spec():
    assert get_device_role("一体式新风机组") is DeviceRole.FRESH_AIR_UNIT

    spec = find_equipment_spec("一体式新风机组", "MKS02D4Y/C")
    assert spec is not None
    assert spec["type"] == "新风机组"
    assert spec["capacity_kw"] > 0.0
    assert spec["water_flow_m3_h"] > 0.0


def test_saved_project_with_legacy_type_still_resolves_rated_values():
    """用旧类型名构造项目，额定值必须来自目录机型而非内置兜底。"""
    payload = deepcopy(FOUR_FLOOR_FCU_FRESH_AIR_SAMPLE)
    for device in payload["devices"]:
        if device["type"] == "新风机组":
            device["type"] = "一体式新风机组"

    layout = build_project_layout(SystemEvaluateRequest(**payload)).layout
    fresh_units = [
        node
        for node in layout.devices
        if node.rated.role is DeviceRole.FRESH_AIR_UNIT
    ]
    assert fresh_units
    for node in fresh_units:
        # 目录机型：MKS 系列冷量在 29~185 kW，且带水侧流量
        assert node.rated.capacity_kw is not None and node.rated.capacity_kw > 20.0
        assert node.rated.flow_m3_h is not None and node.rated.flow_m3_h > 0.0


def test_rated_resolution_accepts_legacy_type_name():
    rated = resolve_rated_spec_by_type("一体式新风机组", "MKS02D4Y/C")
    assert rated.role is DeviceRole.FRESH_AIR_UNIT
    assert rated.capacity_kw == 29.0
