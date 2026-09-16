import logging
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.calculators.air import calculate_air
from app.calculators.air_network import calculate_air_network
from app.calculators.energy import simulate_energy
from app.calculators.hydraulic_network import calculate_hydraulic_network
from app.calculators.load import calculate_load
from app.calculators.simulation import simulate_steady_state
from app.calculators.topology import analyze_topology
from app.calculators.water import calculate_water
from app.data.catalog_import import import_catalog
from app.data.catalog_quality import build_catalog_quality_report
from app.data.equipment import (
    delete_equipment_item,
    load_equipment_catalog,
    save_equipment_catalog,
    strip_unity_internals,
    upsert_equipment_item,
)
from app.data.field_labels import keys_to_zh
from app.data.project_store import (
    add_project_history,
    delete_project,
    get_project,
    list_project_history,
    list_projects,
    save_project,
)
from app.data.rules import load_rules
from app.data.unity_sync import sync_device_library
from app.diagnostics import DiagnosticsResponse, read_diagnostics
from app.equipment_knowledge import (
    get_equipment_knowledge,
    list_equipment_knowledge,
)
from app.equipment_selection import (
    SelectionRequirementsUnavailableError,
    recommend_equipment,
)
from app.project_validation import validate_project_data
from app.realtime import (
    ProjectEditRequiresStopError,
    RuntimeConfigRequiresStopError,
    RuntimeConfigValidationError,
    cleanup_project_runtime,
    get_device_states,
    get_pipe_states,
    get_rated_devices,
    get_realtime_history,
    get_realtime_state,
    get_room_states,
    get_runtime_controls,
    get_simulation_config,
    invalidate_project_runtime_caches,
    is_simulation_running,
    reset_runtime_controls,
    set_runtime_controls,
    set_simulation_config,
    start_simulation,
    stop_simulation,
    subscribe,
    update_realtime_state,
)
from app.reporting import build_report
from app.room_bindings import ProjectRoomBindingError
from app.samples import SAMPLES
from app.schemas import (
    AirNetworkResult,
    AirRequest,
    AirResult,
    CatalogImportRequest,
    CatalogImportResult,
    DevicePortsResult,
    DevicePortView,
    EnergySimulationRequest,
    EnergySimulationResult,
    EquipmentKnowledgeDetail,
    EquipmentKnowledgeSummary,
    HydraulicNetworkResult,
    Issue,
    LoadRequest,
    LoadResult,
    ProjectHistoryEntry,
    ProjectRecord,
    ProjectSaveRequest,
    ProjectSummary,
    ProjectValidationResult,
    RatedDevicesResult,
    RealtimeControlRequest,
    RealtimeControlsResetRequest,
    RealtimeControlsResult,
    RealtimeDeviceStatesResult,
    RealtimeHistoryResult,
    RealtimePipeStatesResult,
    RealtimeRoomStatesResult,
    RealtimeSimulationConfig,
    RealtimeSimulationControl,
    RealtimeSystemState,
    RealtimeUpdateRequest,
    ReportResult,
    SelectionRecommendationRequest,
    SelectionRecommendationResult,
    SteadyStateSimulationResult,
    SystemEvaluateRequest,
    SystemEvaluateResult,
    SystemFormCheckResult,
    SystemGenerateRequest,
    SystemGenerateResult,
    TeachingComparisonRequest,
    TeachingComparisonResult,
    TeachingScenarioDescriptor,
    TopologyAnalysisResult,
    WaterRequest,
    WaterResult,
)
from app.steady_solver.port_graph import query_device_port_layout
from app.steady_solver.solver import build_steady_network_result
from app.steady_solver.solver import solve_steady_state as solve_steady_network
from app.system_builder import SystemGenerationUnavailableError, build_system
from app.teaching_scenarios import (
    ScenarioNotApplicableError,
    compare_teaching_scenario,
    list_teaching_scenarios,
)
from app.validators.system import validate_system
from app.validators.system_form import check_system_form

logger = logging.getLogger(__name__)
app = FastAPI(title="HVAC Algorithm Support", version="0.1.0")
STATIC_DIR = Path(__file__).with_name("static")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.exception_handler(ProjectRoomBindingError)
async def room_binding_exception_handler(
    _request: Request, exc: ProjectRoomBindingError
) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={"detail": [issue.model_dump() for issue in exc.issues]},
    )


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/v1/diagnostics", response_model=DiagnosticsResponse)
def diagnostics() -> DiagnosticsResponse:
    return read_diagnostics()


@app.get("/api/v1/project/sample/{sample_name}")
def project_sample(sample_name: str):
    return SAMPLES.get(sample_name, SAMPLES["complete"])


@app.post("/api/v1/project/validate", response_model=ProjectValidationResult)
def project_validate(request: SystemEvaluateRequest) -> ProjectValidationResult:
    return validate_project_data(request)


@app.post("/api/v1/system/form-check", response_model=SystemFormCheckResult)
def system_form_check(request: SystemEvaluateRequest) -> SystemFormCheckResult:
    """系统制式自检：按 `system_form` 检查拓扑是否满足该制式的成立条件。

    只读：不修改项目、不创建或推进仿真会话、不写历史。
    """
    return check_system_form(request)


@app.post(
    "/api/v1/system/generate",
    response_model=SystemGenerateResult,
    responses={
        422: {
            "description": (
                "建筑数据不足（缺房间），或 `system_form` 不是受支持的制式。"
            )
        }
    },
)
def system_generate(request: SystemGenerateRequest) -> SystemGenerateResult:
    """按制式从楼层与房间生成一套完整的系统骨架。

    输入只需要**建筑**（楼层、房间、用途/面积/人数）与制式，输出是可直接
    `POST /api/v1/projects` 保存、或直接送 `/api/v1/system/evaluate` 评估的完整
    项目（设备 + 管路 + 连接 + 房间末端绑定），外加逐条生成决策及其依据。

    **只读**：不落盘、不改任何已保存项目、不创建或推进实时会话。要保存请把
    返回的 `project` 提交给 `POST /api/v1/projects`。
    """
    try:
        return build_system(request)
    except SystemGenerationUnavailableError as exc:
        raise HTTPException(status_code=422, detail=exc.detail) from exc


@app.get("/api/v1/projects", response_model=list[ProjectSummary])
def projects_list() -> list[ProjectSummary]:
    return list_projects()


@app.post("/api/v1/projects", response_model=ProjectRecord)
def projects_save(request: ProjectSaveRequest) -> ProjectRecord:
    try:
        return save_project(request)
    except ProjectRoomBindingError as exc:
        raise _room_binding_http_error(exc) from exc


@app.get("/api/v1/projects/{project_id}", response_model=ProjectRecord)
def projects_get(project_id: str) -> ProjectRecord:
    try:
        return get_project(project_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Project not found.") from exc


@app.get(
    "/api/v1/projects/{project_id}/air-network",
    response_model=AirNetworkResult,
    responses={404: {"description": "Project not found."}},
)
def projects_air_network(project_id: str) -> AirNetworkResult:
    try:
        project = get_project(project_id).project
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Project not found.") from exc
    load_result = _calculate_load_or_none(project)
    return calculate_air_network(
        project,
        cooling_load_kw=(
            load_result.adjusted_cooling_kw if load_result else 0.0
        ),
    )


@app.put(
    "/api/v1/projects/{project_id}",
    response_model=ProjectRecord,
    responses={409: {"description": "Stop the simulation before editing the project."}},
)
def projects_update(project_id: str, request: ProjectSaveRequest) -> ProjectRecord:
    # 必须先于 save_project 判定：编辑会重建 session，落盘后再拒就会留下
    # "改动已存、仿真还在按旧布局跑"的中间态。
    if is_simulation_running(project_id):
        raise HTTPException(
            status_code=409, detail=ProjectEditRequiresStopError.detail
        )
    try:
        record = save_project(request, project_id=project_id)
    except ProjectRoomBindingError as exc:
        raise _room_binding_http_error(exc) from exc
    invalidate_project_runtime_caches(project_id)
    return record


@app.delete(
    "/api/v1/projects/{project_id}",
    responses={404: {"description": "Project not found."}},
)
async def projects_delete(project_id: str) -> dict[str, bool]:
    # 先确认存在再清理：否则删一个不存在的项目会把 404 变成 500。
    try:
        get_project(project_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Project not found.") from exc
    await cleanup_project_runtime(project_id)
    return {"deleted": delete_project(project_id)}


@app.post(
    "/api/v1/projects/{project_id}/duplicate",
    response_model=ProjectRecord,
    responses={404: {"description": "Project not found."}},
)
def projects_duplicate(project_id: str) -> ProjectRecord:
    """复制项目为副本（含楼层/房间/设备/管路与评估历史）。"""
    try:
        source = get_project(project_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Project not found.") from exc
    copy_name = f"{source.name} 副本"
    existing = {item.name for item in list_projects()}
    if copy_name in existing:
        index = 2
        while f"{copy_name} {index}" in existing:
            index += 1
        copy_name = f"{copy_name} {index}"
    return save_project(
        ProjectSaveRequest(
            name=copy_name,
            description=source.description,
            tags=source.tags,
            project=source.project,
        )
    )


@app.post("/api/v1/projects/{project_id}/evaluate", response_model=SystemEvaluateResult)
def projects_evaluate(project_id: str) -> SystemEvaluateResult:
    try:
        record = get_project(project_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Project not found.") from exc
    result = _evaluate(record.project)
    add_project_history(project_id, result)
    return result


@app.get("/api/v1/projects/{project_id}/history", response_model=list[ProjectHistoryEntry])
def projects_history(project_id: str) -> list[ProjectHistoryEntry]:
    try:
        return list_project_history(project_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Project not found.") from exc


@app.post("/api/v1/realtime/update", response_model=RealtimeSystemState)
async def realtime_update(request: RealtimeUpdateRequest) -> RealtimeSystemState:
    return await update_realtime_state(request)


@app.get("/api/v1/realtime/state/{project_id}", response_model=RealtimeSystemState)
def realtime_state(project_id: str) -> RealtimeSystemState:
    return get_realtime_state(project_id)


@app.get(
    "/api/v1/realtime/rooms/{project_id}",
    response_model=RealtimeRoomStatesResult,
    responses={404: {"description": "Project not found."}},
)
def realtime_rooms(project_id: str) -> RealtimeRoomStatesResult:
    try:
        return get_room_states(project_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Project not found.") from exc


@app.get(
    "/api/v1/realtime/pipes/{project_id}",
    response_model=RealtimePipeStatesResult,
    responses={404: {"description": "Project not found."}},
)
def realtime_pipes(project_id: str) -> RealtimePipeStatesResult:
    try:
        return get_pipe_states(project_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Project not found.") from exc


@app.get(
    "/api/v1/realtime/devices/{project_id}",
    response_model=RealtimeDeviceStatesResult,
    responses={
        404: {"description": "Project not found or unknown device id."},
        422: {"description": "Unknown device type."},
    },
)
def realtime_devices(project_id: str, type: str | None = None) -> RealtimeDeviceStatesResult:
    """全项目逐设备运行读数，可用 `type` 按设备类型筛选。"""
    try:
        return get_device_states(project_id, device_type=type)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Project not found.") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get(
    "/api/v1/realtime/devices/{project_id}/{device_id}",
    response_model=RealtimeDeviceStatesResult,
    responses={404: {"description": "Project or device not found."}},
)
def realtime_device_detail(project_id: str, device_id: str) -> RealtimeDeviceStatesResult:
    """单台设备的运行读数。"""
    try:
        return get_device_states(project_id, device_id=device_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Project not found.") from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Device not found.") from exc


@app.get("/api/v1/realtime/history/{project_id}", response_model=RealtimeHistoryResult)
def realtime_history(project_id: str) -> RealtimeHistoryResult:
    return get_realtime_history(project_id)


@app.post(
    "/api/v1/realtime/control/{project_id}",
    response_model=RealtimeControlsResult,
)
def realtime_control_set(
    project_id: str, request: RealtimeControlRequest
) -> RealtimeControlsResult:
    try:
        return set_runtime_controls(project_id, request)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Project not found.") from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Device not found.") from exc
    except ProjectRoomBindingError as exc:
        raise _room_binding_http_error(exc) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get(
    "/api/v1/realtime/controls/{project_id}",
    response_model=RealtimeControlsResult,
)
def realtime_controls_get(project_id: str) -> RealtimeControlsResult:
    try:
        return get_runtime_controls(project_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Project not found.") from exc


@app.post(
    "/api/v1/realtime/controls/reset/{project_id}",
    response_model=RealtimeControlsResult,
)
def realtime_controls_reset(
    project_id: str, request: RealtimeControlsResetRequest
) -> RealtimeControlsResult:
    try:
        return reset_runtime_controls(project_id, request)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Project not found.") from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Device not found.") from exc


@app.get(
    "/api/v1/projects/{project_id}/devices/rated",
    response_model=RatedDevicesResult,
)
def projects_rated_devices(project_id: str) -> RatedDevicesResult:
    try:
        return get_rated_devices(project_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Project not found.") from exc


@app.get(
    "/api/v1/knowledge/equipment",
    response_model=list[EquipmentKnowledgeSummary],
)
def equipment_knowledge_list() -> list[EquipmentKnowledgeSummary]:
    return list_equipment_knowledge()


@app.get(
    "/api/v1/knowledge/equipment/{knowledge_id}",
    response_model=EquipmentKnowledgeDetail,
    responses={404: {"description": "Equipment knowledge not found."}},
)
def equipment_knowledge_detail(knowledge_id: str) -> EquipmentKnowledgeDetail:
    detail = get_equipment_knowledge(knowledge_id)
    if detail is None:
        raise HTTPException(
            status_code=404,
            detail="Equipment knowledge not found.",
        )
    return detail


@app.get(
    "/api/v1/teaching/scenarios",
    response_model=list[TeachingScenarioDescriptor],
)
def teaching_scenarios_get() -> list[TeachingScenarioDescriptor]:
    return list_teaching_scenarios()


@app.post(
    "/api/v1/teaching/compare/{project_id}",
    response_model=TeachingComparisonResult,
)
def teaching_scenario_compare(
    project_id: str, request: TeachingComparisonRequest
) -> TeachingComparisonResult:
    try:
        return compare_teaching_scenario(project_id, request.scenario_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Project not found.") from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Scenario not found.") from exc
    except ScenarioNotApplicableError as exc:
        raise HTTPException(status_code=422, detail=exc.detail) from exc


@app.post(
    "/api/v1/selection/recommend/{project_id}",
    response_model=SelectionRecommendationResult,
    responses={404: {"description": "Project not found."}},
)
def selection_recommend(
    project_id: str, request: SelectionRecommendationRequest
) -> SelectionRecommendationResult:
    try:
        return recommend_equipment(project_id, request.target_role)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Project not found.") from exc
    except SelectionRequirementsUnavailableError as exc:
        raise HTTPException(status_code=422, detail=exc.detail) from exc


@app.post("/api/v1/realtime/simulate/start/{project_id}", response_model=RealtimeSimulationControl)
async def realtime_simulate_start(project_id: str, config: RealtimeSimulationConfig | None = Body(default=None)) -> RealtimeSimulationControl:  # noqa: B008 - FastAPI 要求以 Body(default=...) 声明可选请求体
    try:
        return await start_simulation(project_id, config)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Project not found.") from exc
    except ProjectRoomBindingError as exc:
        raise _room_binding_http_error(exc) from exc
    except RuntimeConfigRequiresStopError as exc:
        raise HTTPException(status_code=409, detail=exc.detail) from exc
    except RuntimeConfigValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.detail) from exc


def _room_binding_http_error(exc: ProjectRoomBindingError) -> HTTPException:
    return HTTPException(
        status_code=422,
        detail=[issue.model_dump() for issue in exc.issues],
    )


@app.get(
    "/api/v1/realtime/simulate/config/{project_id}",
    response_model=RealtimeSimulationConfig,
    responses={404: {"description": "Project not found."}},
)
def realtime_simulate_config_get(project_id: str) -> RealtimeSimulationConfig:
    try:
        get_project(project_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Project not found.") from exc
    return get_simulation_config(project_id)


@app.post(
    "/api/v1/realtime/simulate/config/{project_id}",
    response_model=RealtimeSimulationConfig,
    responses={404: {"description": "Project not found."}},
)
def realtime_simulate_config_set(project_id: str, config: RealtimeSimulationConfig) -> RealtimeSimulationConfig:
    try:
        get_project(project_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Project not found.") from exc
    try:
        return set_simulation_config(project_id, config)
    except RuntimeConfigRequiresStopError as exc:
        raise HTTPException(status_code=409, detail=exc.detail) from exc
    except RuntimeConfigValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.detail) from exc


@app.post("/api/v1/realtime/simulate/stop/{project_id}", response_model=RealtimeSimulationControl)
async def realtime_simulate_stop(project_id: str) -> RealtimeSimulationControl:
    try:
        return await stop_simulation(project_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Project not found.") from exc


@app.websocket("/ws/realtime/{project_id}")
async def realtime_ws(websocket: WebSocket, project_id: str):
    await subscribe(project_id, websocket)


@app.get("/api/v1/catalog/equipment")
def equipment_catalog():
    # unity_entries 是同步内部结构，不对前端暴露；placement_only 保留供 UI 过滤摆放件。
    return [keys_to_zh(strip_unity_internals(item)) for item in load_equipment_catalog()]


@app.get("/api/v1/catalog/equipment/quality")
def equipment_catalog_quality():
    return build_catalog_quality_report()


@app.get(
    "/api/v1/devices/ports",
    response_model=DevicePortsResult,
    responses={404: {"description": "Unknown device type."}},
)
def devices_ports(type: str, model: str | None = None) -> DevicePortsResult:
    # 端口布局只跟设备类型有关，与具体型号无关；model 仅用于精确命中目录
    # 规格（决定 source/catalog 与 model_name 回显），查不到时退回类型解析。
    layout = query_device_port_layout(type, model)
    if layout is None:
        raise HTTPException(status_code=404, detail=f"设备库中找不到设备类型：{type}")
    return DevicePortsResult(
        device_type=type,
        model_name=layout.model_name or model,
        role=layout.role,
        source=layout.source,
        dynamic_ports=layout.dynamic_ports,
        ports=[
            DevicePortView(
                port_index=index,
                direction=entry.direction,
                medium=entry.medium,
                optional=entry.conditional,
            )
            for index, entry in enumerate(layout.entries)
        ],
    )


@app.put("/api/v1/catalog/equipment")
def equipment_catalog_replace(items: list[dict]):
    return save_equipment_catalog(items)


@app.post("/api/v1/catalog/equipment/sync-unity")
def equipment_catalog_sync_unity():
    """把 Unity DeviceLibrary.json 并入主库，并从主库回写该文件（字节稳定）。"""
    return sync_device_library()


@app.post("/api/v1/catalog/equipment/item")
def equipment_catalog_upsert(item: dict):
    return upsert_equipment_item(item)


@app.delete("/api/v1/catalog/equipment/item/{device_type}/{name}")
def equipment_catalog_delete(device_type: str, name: str):
    return {"deleted": delete_equipment_item(device_type, name)}


@app.get("/api/v1/catalog/equipment/template")
def equipment_catalog_template():
    return {
        "columns": [
            "type",
            "name",
            "aliases",
            "role",
            "brand",
            "series",
            "capacity_kw",
            "heating_capacity_kw",
            "cooling_capacity_kw",
            "flow_m3_h",
            "source_flow_m3_h",
            "head_m",
            "power_kw",
            "heating_power_kw",
            "cooling_power_kw",
            "pressure_drop_pa",
            "airflow_m3_h",
            "airflow_min_m3_h",
            "airflow_max_m3_h",
            "cop",
            "eer",
            "nominal_capacity_ton",
            "chilled_flow_m3_h",
            "cooling_flow_m3_h",
            "chilled_pressure_drop_kpa",
            "cooling_pressure_drop_kpa",
            "efficiency",
            "motor_efficiency",
            "variable_speed",
            "water_flow_m3_h",
            "water_pressure_drop_kpa",
            "fan_power_kw",
            "external_static_pressure_pa",
            "local_resistance_coefficient",
            "air_pressure_drop_pa",
            "recommended_airflow_m3_h",
            "unit_price",
            "weight_kg",
            "cooling_outdoor_dry_bulb_temp_c",
            "heating_outdoor_dry_bulb_temp_c",
            "cooling_evaporator_inlet_temp_c",
            "cooling_evaporator_outlet_temp_c",
            "cooling_condenser_inlet_temp_c",
            "cooling_condenser_outlet_temp_c",
            "heating_evaporator_inlet_temp_c",
            "heating_evaporator_outlet_temp_c",
            "heating_condenser_inlet_temp_c",
            "heating_condenser_outlet_temp_c",
            "refrigerant",
            "derived_fields",
            "source_url",
            "configurable_series",
            "systems",
            "water_cooled",
        ],
        "example": {
            "type": "冷源机组",
            "name": "30XW-P0312P",
            "aliases": "LG340MCF",
            "role": "cold_source",
            "brand": "Carrier",
            "series": "30XW-P",
            "capacity_kw": 303.5,
            "cop": 5.65,
            "power_kw": 53.8,
            "chilled_flow_m3_h": 47.16,
            "cooling_flow_m3_h": 58.68,
            "chilled_pressure_drop_kpa": 24.9,
            "cooling_pressure_drop_kpa": 51.8,
            "refrigerant": "R134a",
            "systems": "chilled_water;cooling_water",
            "water_cooled": True,
        },
    }


@app.post("/api/v1/catalog/equipment/import", response_model=CatalogImportResult)
def equipment_catalog_import(request: CatalogImportRequest) -> CatalogImportResult:
    return import_catalog(request)


@app.get("/api/v1/rules")
def rules():
    return load_rules()


@app.post("/api/v1/load/calculate", response_model=LoadResult)
def load_calculate(request: LoadRequest) -> LoadResult:
    return calculate_load(request)


@app.post("/api/v1/air/calculate", response_model=AirResult)
def air_calculate(request: AirRequest) -> AirResult:
    return calculate_air(request)


@app.post("/api/v1/water/calculate", response_model=WaterResult)
def water_calculate(request: WaterRequest) -> WaterResult:
    return calculate_water(request)


@app.post("/api/v1/topology/analyze", response_model=TopologyAnalysisResult)
def topology_analyze(request: SystemEvaluateRequest) -> TopologyAnalysisResult:
    load_result = _calculate_load_or_none(request)
    return analyze_topology(request, _flow_map_for_topology(request, load_result))


@app.post("/api/v1/hydraulic-network/calculate", response_model=HydraulicNetworkResult)
def hydraulic_network_calculate(request: SystemEvaluateRequest) -> HydraulicNetworkResult:
    load_result = _calculate_load_or_none(request)
    return calculate_hydraulic_network(
        request,
        cooling_load_kw=load_result.adjusted_cooling_kw if load_result else 0.0,
        heating_load_kw=load_result.adjusted_heating_kw if load_result else 0.0,
    )


@app.post("/api/v1/air-network/calculate", response_model=AirNetworkResult)
def air_network_calculate(request: SystemEvaluateRequest) -> AirNetworkResult:
    load_result = _calculate_load_or_none(request)
    return calculate_air_network(request, cooling_load_kw=load_result.adjusted_cooling_kw if load_result else 0.0)


@app.post("/api/v1/simulation/steady-state", response_model=SteadyStateSimulationResult)
def steady_state_simulation(request: SystemEvaluateRequest) -> SteadyStateSimulationResult:
    load_result = _calculate_load_or_none(request)
    hydraulic = calculate_hydraulic_network(
        request,
        cooling_load_kw=load_result.adjusted_cooling_kw if load_result else 0.0,
        heating_load_kw=load_result.adjusted_heating_kw if load_result else 0.0,
    )
    air_network = calculate_air_network(request, cooling_load_kw=load_result.adjusted_cooling_kw if load_result else 0.0)
    return simulate_steady_state(request, load_result, hydraulic, air_network)


@app.post("/api/v1/energy/simulate", response_model=EnergySimulationResult)
def energy_simulate(request: EnergySimulationRequest) -> EnergySimulationResult:
    return simulate_energy(request)


@app.post("/api/v1/report/generate", response_model=ReportResult)
def report_generate(request: SystemEvaluateRequest) -> ReportResult:
    result = _evaluate(request)
    assert result.report is not None
    return result.report


@app.post("/api/v1/report/download")
def report_download(request: SystemEvaluateRequest) -> JSONResponse:
    result = _evaluate(request)
    assert result.report is not None
    return JSONResponse(
        content=result.report.model_dump(),
        headers={"Content-Disposition": 'attachment; filename="hvac_report.json"'},
    )


@app.post("/api/v1/system/evaluate", response_model=SystemEvaluateResult)
def system_evaluate(request: SystemEvaluateRequest) -> SystemEvaluateResult:
    return _evaluate(request)


def _evaluate(request: SystemEvaluateRequest) -> SystemEvaluateResult:
    load_result = None
    water_result = None
    air_result = None
    hydraulic_network = None
    air_network = None
    topology = None
    simulation = None
    issues = []

    if request.rooms:
        load_result = calculate_load(
            LoadRequest(
                rooms=request.rooms,
                floors=request.floors,
                simultaneity_factor=request.simultaneity_factor,
                safety_factor=request.safety_factor,
            )
        )
        issues.extend(load_result.issues)

    required_cooling_kw = load_result.adjusted_cooling_kw if load_result else 0.0
    required_heating_kw = load_result.adjusted_heating_kw if load_result else 0.0

    # 端口图/高级稳态求解是辅助评估能力，不应让基础负荷、拓扑、
    # 水风计算和教学仿真全部失效。保留严格的输入异常（例如房间绑定）
    # 交给既有 HTTP 语义；高级 solver 自身的运行异常降级为可见 Issue。
    steady_solver_result = None
    try:
        steady_solver_result = solve_steady_network(
            request,
            cooling_load_kw=required_cooling_kw,
            heating_load_kw=required_heating_kw,
        )
    except ProjectRoomBindingError:
        # Binding errors are structural input failures, not an optional solver
        # outage.  Let the shared FastAPI exception handler preserve their
        # stable issue codes and 422 response semantics.
        raise
    except Exception:
        # Keep the exception in the service log without exposing implementation
        # details or request contents through the teaching API response.
        logger.exception("Advanced steady solver unavailable during static evaluation")
        issues.append(
            Issue(
                level="warning",
                code="STEADY_SOLVER_UNAVAILABLE",
                message="Advanced steady solver is unavailable; static results are partial.",
                target="steady_network",
            )
        )
    if steady_solver_result is not None:
        issues.extend(steady_solver_result.issues or [])

    issues.extend(validate_system(request, required_cooling_kw))
    topology = analyze_topology(request, _flow_map_for_topology(request, load_result))
    issues.extend(topology.issues)

    if required_cooling_kw > 0:
        water_result = calculate_water(WaterRequest(load_kw=required_cooling_kw))
        air_result = calculate_air(AirRequest(cooling_load_kw=required_cooling_kw))
        hydraulic_network = calculate_hydraulic_network(
            request,
            cooling_load_kw=required_cooling_kw,
            heating_load_kw=required_heating_kw,
        )
        air_network = calculate_air_network(request, cooling_load_kw=required_cooling_kw)
        simulation = simulate_steady_state(request, load_result, hydraulic_network, air_network)
        issues.extend(water_result.issues)
        issues.extend(air_result.issues)
        issues.extend(hydraulic_network.issues)
        issues.extend(air_network.issues)
        issues.extend(simulation.issues)

    issues = _dedupe_issues(issues)
    recommendations = _recommendations_from_issues(issues)
    report = build_report(request, load_result, hydraulic_network, air_network, issues, recommendations)

    return SystemEvaluateResult(
        load=load_result,
        water=water_result,
        air=air_result,
        hydraulic_network=hydraulic_network,
        air_network=air_network,
        topology=topology,
        simulation=simulation,
        steady_network=(
            build_steady_network_result(steady_solver_result)
            if steady_solver_result is not None
            else None
        ),
        report=report,
        issues=issues,
        recommendations=recommendations,
    )


def _flow_map_for_topology(request: SystemEvaluateRequest, load_result: LoadResult | None) -> dict[str, float]:
    cooling_load_kw = load_result.adjusted_cooling_kw if load_result else 0.0
    heating_load_kw = load_result.adjusted_heating_kw if load_result else 0.0
    chilled = 0.86 * cooling_load_kw / 5.0 if cooling_load_kw > 0 else 0.0
    air = calculate_air(AirRequest(cooling_load_kw=cooling_load_kw)).airflow_m3_h if cooling_load_kw > 0 else 0.0
    fallback_air = sum(pipe.flow_m3_h or 0.0 for pipe in request.pipes if pipe.system == "air")
    return {
        "chilled_water": chilled,
        "cooling_water": chilled * 1.2 if chilled > 0 else 0.0,
        "hot_water": 0.86 * heating_load_kw / 10.0 if heating_load_kw > 0 else 0.0,
        "air": air or fallback_air,
    }


def _calculate_load_or_none(request: SystemEvaluateRequest) -> LoadResult | None:
    if not request.rooms:
        return None
    return calculate_load(
        LoadRequest(
            rooms=request.rooms,
            floors=request.floors,
            simultaneity_factor=request.simultaneity_factor,
            safety_factor=request.safety_factor,
        )
    )


def _recommendations_from_issues(issues):
    mapping = {
        "MISSING_COLD_SOURCE": "Add a cold source device such as a chiller.",
        "MISSING_TERMINAL": "Add terminal equipment such as fan coil units or AHU.",
        "MISSING_PUMP": "Add a circulation pump to the water loop.",
        "MISSING_COOLING_TOWER": "Add a cooling tower if the selected cold source is water-cooled.",
        "COLD_SOURCE_UNDERSIZED": "Increase cold source capacity or reduce design load.",
        "TERMINAL_UNDERSIZED": "Increase terminal equipment capacity.",
        "NO_CONNECTIONS": "Provide device and pipe topology connections for loop validation.",
        "ISOLATED_DEVICE": "Connect isolated equipment into the corresponding water or air system.",
        "ISOLATED_PIPE": "Connect isolated pipe segments or remove unused segments.",
        "DEVICE_SYSTEM_MISMATCH": "Correct mixed-system connections between equipment and pipework.",
        "PIPE_SYSTEM_MISMATCH": "Correct pipe system labels so they match the connected loop.",
        "INCOMPLETE_CHILLED_WATER_PATH": "Connect cold source, pump, and terminal equipment in the chilled-water loop.",
        "INCOMPLETE_COOLING_WATER_PATH": "Connect cold source and cooling tower in the cooling-water loop.",
        "OPEN_WATER_LOOP": "Add return-side water connections to close the loop.",
        "PUMP_FLOW_LOW": "Select a pump with larger rated flow.",
        "PUMP_HEAD_LOW": "Select a pump with larger rated head or reduce network resistance.",
        "TERMINAL_AIRFLOW_LOW": "Increase terminal airflow or add more terminal equipment.",
        "SIM_CAPACITY_DEFICIT": "Increase cold source capacity to meet the steady-state load.",
        "WATER_VELOCITY_HIGH": "Select a larger water pipe diameter.",
        "AIR_VELOCITY_HIGH": "Select a larger duct size.",
        "disconnected_network": "Fix disconnected network components - ensure all devices are connected in a complete loop.",
        "supply_return_mismatch": "Check supply/return port connections - avoid connecting supply to supply or return to return.",
        "incomplete_chilled_water_system": "Add required chilled water system components (chiller).",
        "incomplete_cooling_water_system": "Add required cooling water system components (cooling tower).",
        "isolated_port": "Connect all device ports to the appropriate water or air system.",
        "conservation_violation": "Fix flow conservation violations - check pipe sizing and pump capacity.",
        # 制式规则（app/validators/system_form.py）。面向学生的整改方向用中文，
        # 与该模块的 issue 正文保持一致——学生读的是报告，不是英文键名。
        "SYSTEM_FORM_UNDECLARED": "为项目声明空调系统制式（风机盘管 + 新风 / 全空气），才能校核制式条件。",
        "SYSTEM_FORM_TERMINAL_MISSING": "补上本制式要求的房间末端机组（风机盘管 + 新风用风机盘管，全空气用组合式空调机组）。",
        "SYSTEM_FORM_ROOM_TERMINAL_MISSING": "把本制式的末端机组列进该房间的末端清单，或在房间所在层补装末端。",
        "SYSTEM_FORM_FRESH_AIR_HANDLER_MISSING": "补上新风处理机组（风机盘管 + 新风用新风机组，全空气用组合式空调机组）。",
        "SYSTEM_FORM_ROOM_FRESH_AIR_UNCOVERED": "让承担新风的机组也服务这个房间，否则该房间新风按室外状态直注。",
        "SYSTEM_FORM_FORBIDDEN_TERMINAL": "删掉与本制式不符的末端机组，或把项目制式改成与实际设备一致的形式。",
        "SYSTEM_FORM_TERMINAL_NOT_WATER_CONNECTED": "把末端机组接进冷冻水环路，否则仿真中不产出冷热量。",
        "SYSTEM_FORM_FRESH_AIR_HANDLER_NOT_WATER_CONNECTED": "把新风处理机组接进冷冻水环路，否则新风湿负荷会被丢弃。",
        "SYSTEM_FORM_FRESH_AIR_UNIT_NO_AIR_PATH": "给新风机组接出送风管路，否则处理后的新风送不到房间。",
        "SYSTEM_FORM_AHU_DUCT_INCOMPLETE": "把组合式机组的送风与回风两侧都接上风管，全空气系统需要送回风成环。",
        "SYSTEM_FORM_AHU_OUTLET_MISSING": "在组合式机组的风路上补送/回风口（或风阀），让风管真正与房间连通。",
    }
    recommendations = []
    for issue in issues:
        text = mapping.get(issue.code)
        if text and text not in recommendations:
            recommendations.append(text)
    return recommendations


def _dedupe_issues(issues):
    deduped = []
    seen = set()
    for issue in issues:
        key = (issue.level, issue.code, issue.message, issue.target)
        if key not in seen:
            seen.add(key)
            deduped.append(issue)
    return deduped
