"""历史设备类型名的向后兼容

目录里 `一体式新风机组` 改名为 `新风机组` 后，改名之前保存的项目仍带旧类型名。
若不做归一，这些项目会静默退化：角色解析不到（端口布局错乱）、额定参数掉进
内置兜底（冷量/水量与实际机型无关）。这里锁定三个解析入口都能识别旧名。
"""

from copy import deepcopy

from fastapi.testclient import TestClient

from app.data.equipment import find_equipment_spec, resolve_device_type
from app.main import app
from app.samples import FOUR_FLOOR_FCU_FRESH_AIR_SAMPLE
from app.schemas import SystemEvaluateRequest
from app.simcore.project import build_project_layout
from app.simcore.rated import resolve_rated_spec_by_type
from app.unity_catalog import DeviceRole, get_device_role

client = TestClient(app)


def test_legacy_type_name_normalizes_to_current():
    assert resolve_device_type("一体式新风机组") == "新风机组"
    # 现名与未知类型原样返回
    assert resolve_device_type("新风机组") == "新风机组"
    assert resolve_device_type("不存在类型") == "不存在类型"
    assert resolve_device_type(None) == ""


def test_surrounding_whitespace_is_stripped_before_alias_lookup():
    """空白必须先剥离再查别名表

    设备类型在界面上是自由文本输入框（`BuildingView.vue` 的类型列，带 datalist
    但可任意填写），尾随空格是常见输入。此前后端不 strip，而前端
    `canonicalDeviceType` 会 trim，于是同一份草稿出现"前端认为合法、后端报
    `ROOM_BINDING_UNSUPPORTED_DEVICE`"的分歧——与原缺陷同类的假错误，
    只是方向相反。后端是权威口径，两端必须对同一输入给出同一判定。
    """
    # 历史名 + 空白
    assert resolve_device_type(" 一体式新风机组 ") == "新风机组"
    assert resolve_device_type("一体式新风机组\n") == "新风机组"
    # 现名 + 空白（此前会被当成未知类型）
    assert resolve_device_type(" 新风机组 ") == "新风机组"
    assert resolve_device_type("\t风机盘管") == "风机盘管"
    # 纯空白退化为空串，与 None 同路径
    assert resolve_device_type("   ") == ""


def test_padded_legacy_type_binding_is_accepted_by_the_api():
    """带空白的旧类型名不得被房间绑定校验拒绝

    这是前后端分歧的**接口级**表现：`fau` 是末端，房间绑定它必须合法。
    """
    payload = deepcopy(FOUR_FLOOR_FCU_FRESH_AIR_SAMPLE)
    for device in payload["devices"]:
        if device["type"] == "新风机组":
            device["type"] = " 一体式新风机组 "

    response = client.post("/api/v1/project/validate", json=payload)
    assert response.status_code == 200
    codes = [issue["code"] for issue in response.json()["issues"]]
    assert "ROOM_BINDING_UNSUPPORTED_DEVICE" not in codes, (
        f"带空白的旧类型名被误判为非末端：{codes}"
    )


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
