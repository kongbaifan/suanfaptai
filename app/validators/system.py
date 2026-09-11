from collections import Counter

from app.data.equipment import capacity_for_device, role_for_device
from app.schemas import DeviceInput, Issue, SystemEvaluateRequest
from app.validators.floors import validate_floors
from app.validators.topology import validate_topology


def device_capacity(device: DeviceInput) -> float:
    return capacity_for_device(device)


def validate_system(request: SystemEvaluateRequest, required_cooling_kw: float) -> list[Issue]:
    issues: list[Issue] = []
    type_counts = Counter(device.type for device in request.devices)
    issues.extend(validate_floors(request))

    if not request.rooms:
        issues.append(Issue(level="warning", code="NO_ROOMS", message="No room data was provided."))
    if not request.devices:
        issues.append(Issue(level="error", code="NO_DEVICES", message="No equipment was provided."))

    roles = [role_for_device(device) for device in request.devices]
    has_cold_source = "cold_source" in roles or any(t in type_counts for t in ("冷源机组", "冷水机组"))
    has_terminal = "terminal" in roles or any(t in type_counts for t in ("风机盘管", "组合式空调机组", "新风机组"))
    has_pump = "pump" in roles or type_counts["循环泵"] > 0
    has_cooling_tower = "cooling_tower" in roles or type_counts["冷却塔"] > 0

    if not has_cold_source:
        issues.append(Issue(level="error", code="MISSING_COLD_SOURCE", message="Cold source equipment is missing."))
    if not has_terminal:
        issues.append(Issue(level="error", code="MISSING_TERMINAL", message="Terminal air-conditioning equipment is missing."))
    if not has_pump:
        issues.append(Issue(level="warning", code="MISSING_PUMP", message="Circulation pump is missing."))
    if has_cold_source and not has_cooling_tower:
        issues.append(Issue(level="warning", code="MISSING_COOLING_TOWER", message="Cooling tower is missing for water-cooled system assumption."))

    cold_capacity_kw = sum(device_capacity(device) for device in request.devices if role_for_device(device) == "cold_source" or device.type in ("冷源机组", "冷水机组"))
    terminal_capacity_kw = sum(device_capacity(device) for device in request.devices if role_for_device(device) == "terminal" or device.type in ("风机盘管", "组合式空调机组", "新风机组"))

    if required_cooling_kw > 0 and cold_capacity_kw and cold_capacity_kw < required_cooling_kw:
        issues.append(
            Issue(
                level="error",
                code="COLD_SOURCE_UNDERSIZED",
                message=f"Cold source capacity {cold_capacity_kw:.1f} kW is below required {required_cooling_kw:.1f} kW.",
            )
        )
    if required_cooling_kw > 0 and terminal_capacity_kw and terminal_capacity_kw < required_cooling_kw:
        issues.append(
            Issue(
                level="warning",
                code="TERMINAL_UNDERSIZED",
                message=f"Terminal capacity {terminal_capacity_kw:.1f} kW is below required {required_cooling_kw:.1f} kW.",
            )
        )

    if not request.connections:
        issues.append(Issue(level="warning", code="NO_CONNECTIONS", message="No topology connections were provided."))
    else:
        issues.extend(validate_topology(request))

    return issues
