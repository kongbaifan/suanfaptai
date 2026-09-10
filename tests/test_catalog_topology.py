from app.data.equipment import capacity_for_device, role_for_device, systems_for_device
from app.schemas import DeviceInput, SystemEvaluateRequest
from app.validators.system import validate_system


def test_catalog_matches_exact_equipment_capacity():
    device = DeviceInput(id="chiller", type="冷源机组", name="LG440MCF")

    assert capacity_for_device(device) == 366.3
    assert role_for_device(device) == "cold_source"
    assert systems_for_device(device) == {"chilled_water", "cooling_water"}


def test_topology_detects_pipe_system_mismatch():
    request = SystemEvaluateRequest(
        devices=[
            DeviceInput(id="chiller", type="冷源机组", name="LG340MCF"),
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
            DeviceInput(id="chiller", type="冷源机组", name="LG340MCF"),
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
