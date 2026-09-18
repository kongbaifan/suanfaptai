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


def test_chiller_synonym_resolves_like_the_canonical_type():
    """「冷水机组」是长期并行的同义写法，必须与「冷源机组」等价

    此前该同义词只存在于 `app/simcore/project.py` 的内部表里，没有被并进
    `app/data/equipment.py` 的权威表，于是：
    - `get_device_role("冷水机组")` -> None（端口布局失去角色）
    - `find_equipment_spec` -> None，额定参数掉进 builtin_fallback
      （实测 capacity_kw=None），评估报 `UNKNOWN_DEVICE_CATALOG`。
    而 simcore 侧靠自己的表仍能识别——同一份输入两套判定。
    """
    assert resolve_device_type("冷水机组") == "冷源机组"
    assert get_device_role("冷水机组") is DeviceRole.CHILLER

    exact = resolve_rated_spec_by_type("冷水机组", "SCWE-90H+B")
    canonical = resolve_rated_spec_by_type("冷源机组", "SCWE-90H+B")
    assert exact.origin == "catalog", f"应命中目录，实际 {exact.origin}"
    assert exact.role is DeviceRole.CHILLER
    assert exact.capacity_kw == canonical.capacity_kw
    assert exact.capacity_kw is not None and exact.capacity_kw > 0.0


def test_chiller_synonym_does_not_raise_unknown_device_catalog():
    """接口级：用「冷水机组」不得再报 UNKNOWN_DEVICE_CATALOG

    该告警的语义是"目录里没有这台设备"，而冷源机型在目录里是有的——
    报出来会让学习者以为型号选错了。
    """
    payload = deepcopy(FOUR_FLOOR_FCU_FRESH_AIR_SAMPLE)
    for device in payload["devices"]:
        if device["type"] == "冷源机组":
            device["type"] = "冷水机组"

    body = client.post("/api/v1/system/evaluate", json=payload).json()
    codes = [issue["code"] for issue in body["issues"]]
    assert "UNKNOWN_DEVICE_CATALOG" not in codes, (
        f"「冷水机组」被判为目录缺失：{codes}"
    )
    assert "MISSING_COLD_SOURCE" not in codes, (
        f"「冷水机组」未被认作冷源：{codes}"
    )


def test_alias_table_stays_consistent_with_the_catalog():
    """别名表不变量：键不得是真实目录类型，值必须是真实目录类型

    键若是目录里已有的类型名，归一就会把一种**真设备**改写成另一种，
    属静默串型；值若不在目录里，归一后依然解析不到，等于白加一条。
    两条都断言，防止后续往表里加条目时出错。
    """
    import json
    from pathlib import Path

    from app.data.equipment import LEGACY_DEVICE_TYPE_ALIASES

    catalog_path = (
        Path(__file__).resolve().parents[1] / "app" / "data" / "equipment_catalog.json"
    )
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))

    def _types(node):
        if isinstance(node, dict):
            if isinstance(node.get("type"), str):
                yield node["type"]
            for value in node.values():
                yield from _types(value)
        elif isinstance(node, list):
            for value in node:
                yield from _types(value)

    known = {name.strip() for name in _types(catalog)}
    assert LEGACY_DEVICE_TYPE_ALIASES, "别名表不应为空"

    colliding = sorted(k for k in LEGACY_DEVICE_TYPE_ALIASES if k in known)
    assert not colliding, f"别名键与真实目录类型冲突，会串型：{colliding}"

    orphan = sorted(
        v for v in set(LEGACY_DEVICE_TYPE_ALIASES.values()) if v not in known
    )
    assert not orphan, f"别名值不是目录类型，归一后仍解析不到：{orphan}"
