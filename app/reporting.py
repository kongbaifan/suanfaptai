from app.data.equipment import capacity_for_device, flow_for_device, head_for_device, role_for_device
from app.schemas import (
    AirNetworkResult,
    HydraulicNetworkResult,
    Issue,
    LoadResult,
    ReportResult,
    SystemEvaluateRequest,
)


def build_report(
    request: SystemEvaluateRequest,
    load: LoadResult | None,
    hydraulic: HydraulicNetworkResult | None,
    air: AirNetworkResult | None,
    issues: list[Issue],
    recommendations: list[str],
) -> ReportResult:
    return ReportResult(
        summary={
            "floor_count": len(request.floors) if request.floors else 1,
            "room_count": len(request.rooms),
            "device_count": len(request.devices),
            "pipe_count": len(request.pipes),
            "connection_count": len(request.connections),
            "cooling_load_kw": load.adjusted_cooling_kw if load else 0.0,
            "heating_load_kw": load.adjusted_heating_kw if load else 0.0,
            "floor_loads": [floor.model_dump() for floor in load.floor_loads] if load else [],
        },
        equipment=_equipment_summary(request),
        hydraulic=_hydraulic_summary(hydraulic),
        air=_air_summary(air),
        issues=issues,
        recommendations=recommendations,
    )


def _equipment_summary(request: SystemEvaluateRequest) -> dict:
    by_role: dict[str, dict[str, float]] = {}
    for device in request.devices:
        role = role_for_device(device) or "unknown"
        item = by_role.setdefault(role, {"count": 0, "capacity_kw": 0.0, "flow_m3_h": 0.0, "head_m_max": 0.0})
        item["count"] += device.quantity
        item["capacity_kw"] += capacity_for_device(device)
        item["flow_m3_h"] += flow_for_device(device)
        item["head_m_max"] = max(item["head_m_max"], head_for_device(device))
    return by_role


def _hydraulic_summary(hydraulic: HydraulicNetworkResult | None) -> dict | None:
    if not hydraulic:
        return None
    return {
        "total_flow_by_system_m3_h": hydraulic.total_flow_by_system_m3_h,
        "pipe_count": len(hydraulic.pipes),
        "path_count": len(hydraulic.paths),
        "most_unfavorable_pipe_id": hydraulic.most_unfavorable_pipe_id,
        "most_unfavorable_path_id": hydraulic.most_unfavorable_path_id,
        "required_pump_head_m": hydraulic.required_pump_head_m,
        "pump_check": hydraulic.pump_check.model_dump() if hydraulic.pump_check else None,
        "balancing": hydraulic.balancing,
        "floor_flow_summary": hydraulic.floor_flow_summary,
    }


def _air_summary(air: AirNetworkResult | None) -> dict | None:
    if not air:
        return None
    return {
        "total_airflow_m3_h": air.total_airflow_m3_h,
        "duct_count": len(air.ducts),
        "path_count": len(air.paths),
        "most_unfavorable_path_id": air.most_unfavorable_path_id,
        "required_fan_pressure_pa": air.required_fan_pressure_pa,
        "fan_check": air.fan_check.model_dump() if air.fan_check else None,
        "balancing": air.balancing.model_dump(),
        "solver": air.solver.model_dump(),
        "node_balances": [item.model_dump() for item in air.node_balances],
        "ducts": [item.model_dump() for item in air.ducts],
        "paths": [item.model_dump() for item in air.paths],
        "floor_airflow_summary": air.floor_airflow_summary,
    }
