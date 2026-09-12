from app.data.equipment import (
    capacity_for_device,
    find_equipment_spec,
    head_for_device,
    role_for_device,
    systems_for_device,
)
from app.schemas import DeviceInput, SystemEvaluateRequest
from app.validators.system import validate_system


def test_catalog_matches_exact_equipment_capacity():
    device = DeviceInput(id="chiller", type="冷源机组", name="SCWE-110H+B")

    assert capacity_for_device(device) == 379.4
    assert role_for_device(device) == "cold_source"
    assert systems_for_device(device) == {"chilled_water", "cooling_water"}


def test_topology_detects_pipe_system_mismatch():
    request = SystemEvaluateRequest(
        devices=[
            DeviceInput(id="chiller", type="冷源机组", name="SCWE-90H+B"),
            DeviceInput(id="pump", type="循环泵", name="EC80"),
            DeviceInput(id="terminal", type="风机盘管", name="MCW1000V", quantity=3),
            DeviceInput(id="tower", type="冷却塔", name="CT-100"),
        ],
        pipes=[
            {"id": "pipe_bad", "system": "air", "length_m": 10},
        ],
        connections=[
            {"from_id": "chiller", "to_id": "pump", "system": "chilled_water"},
            {"from_id": "pump", "to_id": "pipe_bad", "system": "chilled_water"},
            {"from_id": "pipe_bad", "to_id": "terminal", "system": "chilled_water"},
        ],
    )

    issues = validate_system(request, required_cooling_kw=10)
    codes = {issue.code for issue in issues}

    assert "PIPE_SYSTEM_MISMATCH" in codes
    assert "INCOMPLETE_COOLING_WATER_PATH" in codes


def test_complete_closed_topology_has_no_topology_error():
    request = SystemEvaluateRequest(
        devices=[
            DeviceInput(id="chiller", type="冷源机组", name="SCWE-90H+B"),
            DeviceInput(id="pump", type="循环泵", name="EC80"),
            DeviceInput(id="terminal", type="风机盘管", name="MCW1000V", quantity=3),
            DeviceInput(id="tower", type="冷却塔", name="CT-100"),
        ],
        pipes=[
            {"id": "pipe_cw", "system": "chilled_water", "length_m": 10},
            {"id": "pipe_ct", "system": "cooling_water", "length_m": 10},
        ],
        connections=[
            {"from_id": "chiller", "to_id": "pump", "system": "chilled_water"},
            {"from_id": "pump", "to_id": "pipe_cw", "system": "chilled_water"},
            {"from_id": "pipe_cw", "to_id": "terminal", "system": "chilled_water"},
            {"from_id": "terminal", "to_id": "chiller", "system": "chilled_water"},
            {"from_id": "chiller", "to_id": "pipe_ct", "system": "cooling_water"},
            {"from_id": "pipe_ct", "to_id": "tower", "system": "cooling_water"},
            {"from_id": "tower", "to_id": "chiller", "system": "cooling_water"},
        ],
    )

    issues = validate_system(request, required_cooling_kw=10)
    codes = {issue.code for issue in issues}

    assert "INCOMPLETE_CHILLED_WATER_PATH" not in codes
    assert "INCOMPLETE_COOLING_WATER_PATH" not in codes
    assert "OPEN_WATER_LOOP" not in codes
    assert "PIPE_SYSTEM_MISMATCH" not in codes


def test_placement_only_cold_source_still_reports_role():
    """LG340MCF 只有 Unity 摆放条目，但目录里声明了 role，拓扑不能把它当无名设备。

    角色丢失会让系统里"没有冷源"，水力路径枚举直接返回空、扬程退化到兜底分支。
    """
    device = DeviceInput(id="chiller_b1", type="冷源机组", name="LG340MCF")

    assert find_equipment_spec("冷源机组", "LG340MCF") is None
    assert role_for_device(device) == "cold_source"
    assert systems_for_device(device) == {"chilled_water", "cooling_water", "hot_water"}


def test_placement_only_cooling_tower_still_reports_role():
    device = DeviceInput(id="tower", type="冷却塔", name="CT-8")

    assert find_equipment_spec("冷却塔", "CT-8") is None
    assert role_for_device(device) == "cooling_tower"


def test_engineering_row_role_unaffected_by_placement_fallback():
    """工程条目仍应命中自己，不被 placement_only 兜底截胡。"""
    device = DeviceInput(id="chiller", type="冷源机组", name="SCWE-90H+B")

    spec = find_equipment_spec("冷源机组", "SCWE-90H+B")
    assert spec is not None and not spec.get("placement_only")
    assert role_for_device(device) == "cold_source"
    assert capacity_for_device(device) > 0


def test_placement_only_row_does_not_supply_engineering_numbers():
    """拓扑兜底不得渗进额定参数：placement_only 行没有容量/扬程，必须仍是 0。"""
    device = DeviceInput(id="chiller_b1", type="冷源机组", name="LG340MCF")

    assert capacity_for_device(device) == 0.0
    assert head_for_device(device) == 0.0


def test_unknown_device_type_has_no_role():
    device = DeviceInput(id="mystery", type="不存在的类型", name="X-1")

    assert role_for_device(device) is None
    assert systems_for_device(device) == set()
