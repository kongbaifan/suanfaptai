import math
from typing import Literal

from app.building import (
    floor_base_summary,
    implicit_floor_id,
    node_floor_id,
    pipe_floor_id,
)
from app.calculators.air import (
    AIR_DENSITY_KG_M3,
    _area_for_rect,
    _area_for_round,
    calculate_air,
)
from app.calculators.air_tree import solve_air_trees
from app.calculators.network_paths import enumerate_network_paths, pipe_ids_in_path
from app.calculators.topology import analyze_topology
from app.data.equipment import (
    airflow_for_device,
    catalog_number_for_device,
    role_for_device,
)
from app.data.rules import rule_value
from app.schemas import (
    AirNetworkBalancing,
    AirNetworkResult,
    AirNetworkSolverStatus,
    AirRequest,
    DuctCalculation,
    FanCheckResult,
    Issue,
    NetworkPathCalculation,
    SystemEvaluateRequest,
)

MAX_CALCULATION_VALUE = 1_000_000_000_000.0


def calculate_air_network(request: SystemEvaluateRequest, cooling_load_kw: float = 0.0) -> AirNetworkResult:
    ducts = sorted(
        (pipe for pipe in request.pipes if pipe.system == "air"),
        key=lambda item: item.id,
    )
    safe_cooling_load_kw = _finite_nonnegative(cooling_load_kw)
    required_airflow = calculate_air(AirRequest(cooling_load_kw=safe_cooling_load_kw)).airflow_m3_h if safe_cooling_load_kw > 0 else 0.0
    if required_airflow <= 0:
        required_airflow = _safe_fsum(
            _finite_nonnegative(pipe.flow_m3_h)
            for pipe in ducts
        )

    topology = analyze_topology(request, {"air": required_airflow})
    tree_solution = solve_air_trees(
        request,
        required_airflow,
        topology.pipe_flow_m3_h,
    )
    total_airflow = (
        tree_solution.supply_total_m3_h
        or tree_solution.return_total_m3_h
        or required_airflow
    )
    issues: list[Issue] = list(tree_solution.issues)
    calculations: list[DuctCalculation] = []
    for duct in ducts:
        airflow = tree_solution.flow_by_duct_m3_h.get(duct.id, 0.0)
        connected_pressure_pa, connected_local_k = _connected_air_resistance(request, duct.id)
        calc, duct_issues = _calculate_duct(
            duct.id,
            airflow,
            duct.length_m,
            duct.width_mm,
            duct.height_mm,
            duct.diameter_mm,
            duct.local_resistance_coefficient + connected_local_k,
            connected_pressure_pa,
            input_airflow_m3_h=duct.flow_m3_h,
            flow_source=tree_solution.flow_source_by_duct.get(duct.id, "legacy_topology"),
            network_side=tree_solution.side_by_duct.get(duct.id, "unknown"),
        )
        calculations.append(calc)
        issues.extend(duct_issues)

    path_calculations = _path_calculations(request, calculations)
    required_fan_pressure = _required_fan_pressure(calculations, path_calculations)
    fan_check = _build_fan_check(request, total_airflow, required_fan_pressure)
    balancing = _balancing_summary(path_calculations)
    floor_airflow_summary = _floor_airflow_summary(request, calculations)
    issues.extend(_validate_terminal_airflow(request, total_airflow))
    issues.extend(_issues_from_fan_check(fan_check))
    issues.extend(_issues_from_balancing(balancing))
    issues.extend(issue for issue in topology.issues if issue.code not in {"OPEN_WATER_LOOP"})

    return AirNetworkResult(
        total_airflow_m3_h=round(total_airflow, 3),
        ducts=calculations,
        paths=path_calculations,
        required_fan_pressure_pa=required_fan_pressure,
        most_unfavorable_path_id=(
            max(path_calculations, key=lambda item: item.pressure_drop_pa).path_id
            if path_calculations
            else None
        ),
        fan_check=fan_check,
        balancing=balancing,
        solver=AirNetworkSolverStatus(
            method=(
                "directed_tree_aggregation"
                if tree_solution.succeeded
                else "legacy_bounded_fallback"
            ),
            succeeded=tree_solution.succeeded,
            degraded=tree_solution.degraded,
            steps=tree_solution.steps,
            max_flow_residual_m3_h=round(tree_solution.max_residual_m3_h, 6),
            flow_demand_sources=dict(sorted(tree_solution.flow_source_by_duct.items())),
            fallback_used=tree_solution.fallback_used,
        ),
        node_balances=sorted(
            tree_solution.node_balances,
            key=lambda item: (item.network_side, item.root_id, item.node_id),
        ),
        floor_airflow_summary=floor_airflow_summary,
        issues=_dedupe_issues(issues),
    )


def _calculate_duct(
    duct_id,
    airflow_m3_h,
    length_m,
    width_mm,
    height_mm,
    diameter_mm,
    local_k,
    extra_pressure_pa=0.0,
    *,
    input_airflow_m3_h=None,
    flow_source="legacy_topology",
    network_side="unknown",
):
    airflow_m3_h = _finite_flow_value(airflow_m3_h)
    length_m = _finite_nonnegative(length_m)
    diameter_mm = _finite_positive_or_none(diameter_mm)
    width_mm = _finite_positive_or_none(width_mm)
    height_mm = _finite_positive_or_none(height_mm)
    local_k = _finite_nonnegative(local_k)
    extra_pressure_pa = _finite_nonnegative(extra_pressure_pa)
    flow_m3_s = airflow_m3_h / 3600.0
    issues: list[Issue] = []
    area = 0.0
    hydraulic_diameter_m = 0.0
    if diameter_mm:
        area = _area_for_round(diameter_mm)
        hydraulic_diameter_m = diameter_mm / 1000.0
        size = {"type": "round", "diameter_mm": diameter_mm, "area_m2": round(area, 4), "source": "input"}
    elif width_mm and height_mm:
        area = _area_for_rect(width_mm, height_mm)
        hydraulic_diameter_m = 2 * (width_mm / 1000.0) * (height_mm / 1000.0) / ((width_mm / 1000.0) + (height_mm / 1000.0))
        size = {"type": "rectangular", "width_mm": width_mm, "height_mm": height_mm, "area_m2": round(area, 4), "source": "input"}
    if not _valid_geometry(area, hydraulic_diameter_m):
        bounded_load_kw = _bounded_result(
            airflow_m3_h * AIR_DENSITY_KG_M3 * 1005.0 * 10.0 / 3600.0 / 1000.0
        )
        selected = calculate_air(
            AirRequest(cooling_load_kw=max(bounded_load_kw, 0.1))
        ).selected_duct
        area = selected["area_m2"]
        if selected["type"] == "round":
            hydraulic_diameter_m = selected["diameter_mm"] / 1000.0
        else:
            width_m = selected["width_mm"] / 1000.0
            height_m = selected["height_mm"] / 1000.0
            hydraulic_diameter_m = 2 * width_m * height_m / (width_m + height_m)
        size = {**selected, "source": "standard"}
        issues.append(
            Issue(
                level="info",
                code="AIR_DUCT_SIZE_MISSING",
                message="Duct dimensions are missing; a bounded standard size was selected.",
                target=duct_id,
            )
        )

    velocity = _bounded_result(flow_m3_s / area if area > 0 else 0.0)
    dynamic_pressure = _bounded_result(
        AIR_DENSITY_KG_M3 * velocity * velocity / 2.0
    )
    friction_factor = _finite_nonnegative(
        rule_value("pressure_defaults.air_friction_factor", 0.02)
    )
    pressure_drop = _bounded_result(
        friction_factor * (length_m / hydraulic_diameter_m) * dynamic_pressure
        + local_k * dynamic_pressure
        + extra_pressure_pa
    )

    velocity_rule = rule_value("air_velocity_m_s.main_duct", {"min": 2.0, "max": 8.0})
    if velocity > velocity_rule["max"]:
        issues.append(Issue(level="warning", code="AIR_VELOCITY_HIGH", message="Air duct velocity is higher than recommended.", target=duct_id))
    if velocity < velocity_rule["min"] and airflow_m3_h > 0:
        issues.append(Issue(level="info", code="AIR_VELOCITY_LOW", message="Air duct velocity is lower than recommended.", target=duct_id))

    return (
        DuctCalculation(
            duct_id=duct_id,
            airflow_m3_h=round(airflow_m3_h, 3),
            input_airflow_m3_h=(
                round(float(input_airflow_m3_h), 3)
                if _is_finite_nonnegative(input_airflow_m3_h)
                else None
            ),
            flow_source=flow_source,
            network_side=network_side,
            size=size,
            velocity_m_s=round(velocity, 3),
            pressure_drop_pa=round(pressure_drop, 3),
        ),
        issues,
    )


def _validate_terminal_airflow(request: SystemEvaluateRequest, required_airflow_m3_h: float) -> list[Issue]:
    if required_airflow_m3_h <= 0:
        return []
    total_terminal_airflow = _safe_fsum(
        _finite_nonnegative(airflow_for_device(device))
        for device in request.devices
        if role_for_device(device) == "terminal"
    )
    if total_terminal_airflow and total_terminal_airflow < required_airflow_m3_h:
        return [Issue(level="warning", code="TERMINAL_AIRFLOW_LOW", message="Terminal airflow is below calculated supply airflow.")]
    return []


def _path_calculations(request: SystemEvaluateRequest, calculations: list[DuctCalculation]) -> list[NetworkPathCalculation]:
    by_duct = {item.duct_id: item for item in calculations}
    path_items: list[NetworkPathCalculation] = []
    for index, nodes in enumerate(enumerate_network_paths(request, "air"), start=1):
        duct_ids = pipe_ids_in_path(request, nodes, "air")
        pressure_drop = _safe_fsum(
            by_duct[duct_id].pressure_drop_pa
            for duct_id in duct_ids
            if duct_id in by_duct
        )
        path_airflow = min((by_duct[duct_id].airflow_m3_h for duct_id in duct_ids if duct_id in by_duct), default=0.0)
        path_items.append(
            NetworkPathCalculation(
                path_id=f"air_path_{index}",
                system="air",
                nodes=nodes,
                floor_ids=_path_floor_ids(request, nodes),
                pipe_ids=duct_ids,
                pressure_drop_pa=round(pressure_drop, 3),
                head_m=None,
                flow_m3_h=round(path_airflow, 3),
                complete=bool(duct_ids),
            )
        )
    max_pressure_drop = max(
        (item.pressure_drop_pa for item in path_items if item.complete),
        default=0.0,
    )
    for item in path_items:
        item.balancing_pressure_pa = round(
            max(max_pressure_drop - item.pressure_drop_pa, 0.0),
            3,
        )
    return path_items


def _floor_airflow_summary(request: SystemEvaluateRequest, calculations: list[DuctCalculation]) -> dict[str, dict[str, float]]:
    summary = {floor_id: {"air": 0.0, "duct_count": 0} for floor_id in floor_base_summary(request)}
    pipe_map = {pipe.id: pipe for pipe in request.pipes}
    for calc in calculations:
        pipe = pipe_map.get(calc.duct_id)
        if not pipe:
            continue
        floor_id = pipe_floor_id(pipe, implicit=implicit_floor_id(request))
        item = summary.setdefault(floor_id, {"air": 0.0, "duct_count": 0})
        item["air"] = round(
            _safe_fsum((item.get("air", 0.0), calc.airflow_m3_h)),
            3,
        )
        item["duct_count"] += 1
    return summary


def _path_floor_ids(request: SystemEvaluateRequest, nodes: list[str]) -> list[str]:
    floor_ids = []
    for node in nodes:
        floor_id = node_floor_id(request, node)
        if floor_id not in floor_ids:
            floor_ids.append(floor_id)
    return floor_ids


def _required_fan_pressure(calculations: list[DuctCalculation], paths: list[NetworkPathCalculation]) -> float:
    path_drops = [path.pressure_drop_pa for path in paths if path.pressure_drop_pa > 0]
    if path_drops:
        return round(max(path_drops), 3)
    return round(_safe_fsum(item.pressure_drop_pa for item in calculations), 3)


def _build_fan_check(request: SystemEvaluateRequest, required_airflow_m3_h: float, required_pressure_pa: float) -> FanCheckResult | None:
    terminals = [device for device in request.devices if role_for_device(device) == "terminal"]
    installed_airflow = _safe_fsum(
        _finite_nonnegative(airflow_for_device(device))
        for device in terminals
    )
    available_pressure = _finite_nonnegative(
        request.design_options.get("fan_static_pressure_pa", 0.0)
    )
    for device in terminals:
        available_pressure = max(
            available_pressure,
            _finite_nonnegative(device.pressure_drop_pa),
        )
        available_pressure = max(
            available_pressure,
            _finite_nonnegative(
                catalog_number_for_device(
                    device,
                    "external_static_pressure_pa",
                    0.0,
                )
            ),
        )
    airflow_margin = installed_airflow / required_airflow_m3_h if required_airflow_m3_h > 0 and installed_airflow > 0 else None
    pressure_margin = available_pressure / required_pressure_pa if required_pressure_pa > 0 and available_pressure > 0 else None
    status: Literal["ok", "warning", "error"] = "ok"
    if airflow_margin is not None and airflow_margin < 1.0:
        status = "warning"
    if pressure_margin is not None and pressure_margin < 1.05:
        status = "warning"
    return FanCheckResult(
        required_airflow_m3_h=round(required_airflow_m3_h, 3),
        installed_airflow_m3_h=round(installed_airflow, 3),
        required_pressure_pa=round(required_pressure_pa, 3),
        available_pressure_pa=round(available_pressure, 3),
        airflow_margin=round(airflow_margin, 3) if airflow_margin is not None else None,
        pressure_margin=round(pressure_margin, 3) if pressure_margin is not None else None,
        status=status,
    )


def _connected_air_resistance(request: SystemEvaluateRequest, duct_id: str) -> tuple[float, float]:
    devices = {device.id: device for device in request.devices}
    pressure_pa = 0.0
    local_k = 0.0
    for connection in request.connections:
        if connection.from_id != duct_id and connection.to_id != duct_id:
            continue
        device = devices.get(connection.to_id if connection.from_id == duct_id else connection.from_id)
        if not device:
            continue
        role = role_for_device(device)
        pressure_pa += catalog_number_for_device(device, "air_pressure_drop_pa", 0.0) * device.quantity
        pressure_pa += catalog_number_for_device(device, "pressure_drop_pa", 0.0) * device.quantity
        if role in {"air_accessory", "air_terminal", "terminal"}:
            local_k += catalog_number_for_device(device, "local_resistance_coefficient", 0.0) * device.quantity
    return pressure_pa, local_k


def _issues_from_fan_check(check: FanCheckResult | None) -> list[Issue]:
    if not check:
        return []
    issues: list[Issue] = []
    if check.installed_airflow_m3_h > 0 and check.airflow_margin is not None and check.airflow_margin < 1.0:
        issues.append(Issue(level="warning", code="FAN_AIRFLOW_LOW", message="Installed terminal airflow is below required air network airflow."))
    if check.required_pressure_pa > 0 and check.available_pressure_pa <= 0:
        issues.append(Issue(level="info", code="FAN_PRESSURE_UNKNOWN", message="Fan static pressure is not provided; set design_options.fan_static_pressure_pa for pressure check."))
    elif check.pressure_margin is not None and check.pressure_margin < 1.05:
        issues.append(Issue(level="warning", code="FAN_PRESSURE_LOW", message="Fan static pressure is below calculated most unfavorable air path."))
    return issues


def _balancing_summary(paths: list[NetworkPathCalculation]) -> AirNetworkBalancing:
    drops = [path.pressure_drop_pa for path in paths if path.pressure_drop_pa > 0]
    if not drops:
        return AirNetworkBalancing()
    max_drop = max(drops)
    min_drop = min(drops)
    return AirNetworkBalancing(
        path_count=len(drops),
        min_pressure_drop_pa=round(min_drop, 3),
        max_pressure_drop_pa=round(max_drop, 3),
        imbalance_ratio=round((max_drop - min_drop) / max_drop if max_drop > 0 and len(drops) > 1 else 0.0, 3),
        path_balancing_pressure_pa={
            path.path_id: float(path.balancing_pressure_pa or 0.0)
            for path in sorted(paths, key=lambda item: item.path_id)
            if path.pressure_drop_pa > 0
        },
    )


def _issues_from_balancing(balancing: AirNetworkBalancing) -> list[Issue]:
    if balancing.imbalance_ratio > 0.15:
        return [Issue(level="warning", code="AIR_BALANCING_REQUIRED", message="Air path pressure deviation is above 15%; add balancing damper or resize duct branches.")]
    return []


def _dedupe_issues(issues: list[Issue]) -> list[Issue]:
    seen = set()
    deduped = []
    for issue in issues:
        key = (issue.level, issue.code, issue.message, issue.target)
        if key not in seen:
            seen.add(key)
            deduped.append(issue)
    return sorted(
        deduped,
        key=lambda item: (item.code, item.target or "", item.level, item.message),
    )


def _finite_nonnegative(value) -> float:
    try:
        parsed = float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(parsed) or parsed < 0:
        return 0.0
    return min(parsed, MAX_CALCULATION_VALUE)


def _finite_positive_or_none(value) -> float | None:
    parsed = _finite_nonnegative(value)
    return parsed if parsed > 0 else None


def _finite_flow_value(value) -> float:
    try:
        parsed = float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return parsed if math.isfinite(parsed) and parsed >= 0 else 0.0


def _is_finite_nonnegative(value) -> bool:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(parsed) and parsed >= 0


def _bounded_result(value) -> float:
    try:
        parsed = float(value)
    except (OverflowError, TypeError, ValueError):
        return MAX_CALCULATION_VALUE
    if math.isnan(parsed) or parsed < 0:
        return 0.0
    if math.isinf(parsed):
        return MAX_CALCULATION_VALUE
    return min(parsed, MAX_CALCULATION_VALUE)


def _safe_fsum(values) -> float:
    try:
        total = math.fsum(_finite_nonnegative(value) for value in values)
    except OverflowError:
        return MAX_CALCULATION_VALUE
    return _bounded_result(total)


def _valid_geometry(area: float, hydraulic_diameter_m: float) -> bool:
    return (
        math.isfinite(area)
        and area > 0
        and math.isfinite(hydraulic_diameter_m)
        and hydraulic_diameter_m > 0
    )
