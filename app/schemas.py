from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Issue(BaseModel):
    level: Literal["error", "warning", "info"]
    code: str
    message: str
    target: str | None = None


class FloorInput(BaseModel):
    id: str
    name: str | None = None
    level: int = 0
    elevation_m: float = 0.0
    height_m: float = Field(default=3.6, gt=0)


class RoomInput(BaseModel):
    id: str | None = None
    floor_id: str | None = None
    name: str = "room"
    # 房间持有服务它的末端设备清单。方向不能反过来放到 DeviceInput 上：
    # 房间是建筑里的实体，设备装在房间里，删房间应当连同清单一起消失。
    device_ids: list[str] = []
    area_m2: float = Field(gt=0)
    height_m: float = Field(default=3.0, gt=0)
    people: int = Field(default=0, ge=0)
    purpose: str = "office"
    cooling_index_w_m2: float = Field(default=120.0, gt=0)
    heating_index_w_m2: float = Field(default=80.0, gt=0)
    lighting_w_m2: float = Field(default=10.0, ge=0)
    equipment_w_m2: float = Field(default=15.0, ge=0)
    fresh_air_m3_h_person: float = Field(default=30.0, ge=0)
    outdoor_summer_c: float = 35.0
    indoor_summer_c: float = 26.0
    outdoor_winter_c: float = -5.0
    indoor_winter_c: float = 20.0
    envelope_u_w_m2k: float = Field(default=1.2, gt=0)
    exposed_area_factor: float = Field(default=1.4, gt=0)
    window_wall_ratio: float = Field(default=0.25, ge=0, le=0.9)
    orientation: Literal["north", "south", "east", "west", "internal"] = "south"
    indoor_relative_humidity: float = Field(default=0.55, ge=0.1, le=0.9)
    outdoor_relative_humidity: float = Field(default=0.65, ge=0, le=1)


class LoadRequest(BaseModel):
    rooms: list[RoomInput]
    floors: list[FloorInput] = []
    simultaneity_factor: float = Field(default=0.9, gt=0, le=1.0)
    safety_factor: float = Field(default=1.1, gt=0)


class RoomLoadResult(BaseModel):
    id: str | None = None
    floor_id: str
    name: str
    cooling_kw: float
    heating_kw: float
    fresh_air_m3_h: float
    components_kw: dict[str, float]


class FloorLoadResult(BaseModel):
    floor_id: str
    floor_name: str
    level: int
    area_m2: float
    people: int
    room_count: int
    cooling_kw: float
    heating_kw: float
    adjusted_cooling_kw: float
    adjusted_heating_kw: float
    cooling_share: float


class LoadResult(BaseModel):
    total_cooling_kw: float
    total_heating_kw: float
    adjusted_cooling_kw: float
    adjusted_heating_kw: float
    rooms: list[RoomLoadResult]
    floor_loads: list[FloorLoadResult] = []
    issues: list[Issue] = []


class AirRequest(BaseModel):
    cooling_load_kw: float = Field(gt=0)
    supply_return_delta_t_c: float = Field(default=10.0, gt=0)
    target_velocity_m_s: float = Field(default=6.0, gt=0)
    duct_type: Literal["rectangular", "round"] = "rectangular"


class AirResult(BaseModel):
    airflow_m3_h: float
    selected_duct: dict[str, Any]
    actual_velocity_m_s: float
    issues: list[Issue] = []


class WaterRequest(BaseModel):
    load_kw: float = Field(gt=0)
    delta_t_c: float = Field(default=5.0, gt=0)
    target_velocity_m_s: float = Field(default=1.5, gt=0)
    length_m: float = Field(default=30.0, ge=0)
    local_resistance_coefficient: float = Field(default=6.0, ge=0)


class WaterResult(BaseModel):
    flow_m3_h: float
    selected_pipe: dict[str, Any]
    actual_velocity_m_s: float
    pressure_drop_pa: float
    pump_head_m: float
    issues: list[Issue] = []


class DeviceInput(BaseModel):
    id: str
    type: str
    name: str | None = None
    floor_id: str | None = None
    location_type: str | None = None
    capacity_kw: float | None = Field(default=None, ge=0)
    flow_m3_h: float | None = Field(default=None, ge=0)
    head_m: float | None = Field(default=None, ge=0)
    power_kw: float | None = Field(default=None, ge=0)
    pressure_drop_pa: float | None = Field(default=None, ge=0)
    quantity: int = Field(default=1, ge=1)


class PipeInput(BaseModel):
    id: str
    system: Literal["chilled_water", "cooling_water", "hot_water", "air"]
    pipe_type: Literal[
        "main_supply",
        "main_return",
        "branch_supply",
        "branch_return",
        "terminal_branch",
        "riser_supply",
        "riser_return",
        "bypass",
        "unknown",
    ] = "unknown"
    floor_id: str | None = None
    from_floor_id: str | None = None
    to_floor_id: str | None = None
    length_m: float = Field(default=10.0, ge=0)
    flow_m3_h: float | None = Field(default=None, gt=0)
    diameter_mm: float | None = Field(default=None, gt=0)
    width_mm: float | None = Field(default=None, gt=0)
    height_mm: float | None = Field(default=None, gt=0)
    local_resistance_coefficient: float = Field(default=2.0, ge=0)


class ConnectionInput(BaseModel):
    from_id: str
    to_id: str
    system: Literal["chilled_water", "cooling_water", "hot_water", "air"]


class SystemEvaluateRequest(BaseModel):
    floors: list[FloorInput] = []
    rooms: list[RoomInput] = []
    devices: list[DeviceInput] = []
    pipes: list[PipeInput] = []
    connections: list[ConnectionInput] = []
    simultaneity_factor: float = Field(default=0.9, gt=0, le=1.0)
    safety_factor: float = Field(default=1.1, gt=0)
    design_options: dict[str, Any] = {}


class TopologyPath(BaseModel):
    system: str
    name: str
    nodes: list[str]
    floor_ids: list[str] = []
    complete: bool


class TopologyAnalysisResult(BaseModel):
    node_count: int
    edge_count_by_system: dict[str, int]
    components_by_system: dict[str, list[list[str]]]
    open_nodes_by_system: dict[str, list[str]]
    loop_count_by_system: dict[str, int]
    isolated_nodes: list[str]
    paths: list[TopologyPath]
    pipe_flow_m3_h: dict[str, float]
    floor_summary: dict[str, dict[str, Any]] = {}
    vertical_network: dict[str, Any] = {}
    issues: list[Issue] = []


class PipeCalculation(BaseModel):
    pipe_id: str
    system: str
    flow_m3_h: float
    size: dict[str, Any]
    velocity_m_s: float
    pressure_drop_pa: float
    pump_head_m: float | None = None


class NetworkPathCalculation(BaseModel):
    path_id: str
    system: str
    nodes: list[str]
    floor_ids: list[str] = []
    pipe_ids: list[str]
    pressure_drop_pa: float
    head_m: float | None = None
    flow_m3_h: float | None = None
    balancing_pressure_pa: float | None = None
    complete: bool = True


class PumpCheckResult(BaseModel):
    required_flow_m3_h: float
    installed_flow_m3_h: float
    required_head_m: float
    available_head_m: float
    duty_pump_count: int
    standby_pump_count: int
    flow_margin: float | None = None
    head_margin: float | None = None
    status: Literal["ok", "warning", "error"]


class HydraulicNetworkResult(BaseModel):
    total_flow_by_system_m3_h: dict[str, float]
    pipes: list[PipeCalculation]
    paths: list[NetworkPathCalculation] = []
    most_unfavorable_pipe_id: str | None
    most_unfavorable_path_id: str | None = None
    required_pump_head_m: float
    pump_check: PumpCheckResult | None = None
    balancing: dict[str, Any] = {}
    floor_flow_summary: dict[str, dict[str, float]] = {}
    issues: list[Issue] = []


class DuctCalculation(BaseModel):
    duct_id: str
    airflow_m3_h: float
    input_airflow_m3_h: float | None = None
    flow_source: str = "legacy_topology"
    network_side: Literal["supply", "return", "unknown"] = "unknown"
    size: dict[str, Any]
    velocity_m_s: float
    pressure_drop_pa: float


class FanCheckResult(BaseModel):
    required_airflow_m3_h: float
    installed_airflow_m3_h: float
    required_pressure_pa: float
    available_pressure_pa: float
    airflow_margin: float | None = None
    pressure_margin: float | None = None
    status: Literal["ok", "warning", "error"]


class AirNodeBalance(BaseModel):
    node_id: str
    network_side: Literal["supply", "return"]
    root_id: str
    inflow_m3_h: float
    outflow_m3_h: float
    residual_m3_h: float


class AirNetworkSolverStatus(BaseModel):
    method: Literal["directed_tree_aggregation", "legacy_bounded_fallback"]
    succeeded: bool
    degraded: bool
    steps: int = Field(ge=0)
    max_flow_residual_m3_h: float = Field(ge=0)
    flow_demand_sources: dict[str, str] = Field(default_factory=dict)
    fallback_used: bool = False


class AirNetworkBalancing(BaseModel):
    path_count: int = Field(default=0, ge=0)
    min_pressure_drop_pa: float = Field(default=0.0, ge=0)
    max_pressure_drop_pa: float = Field(default=0.0, ge=0)
    imbalance_ratio: float = Field(default=0.0, ge=0)
    path_balancing_pressure_pa: dict[str, float] = Field(default_factory=dict)


class AirNetworkResult(BaseModel):
    total_airflow_m3_h: float
    ducts: list[DuctCalculation]
    paths: list[NetworkPathCalculation] = []
    required_fan_pressure_pa: float
    most_unfavorable_path_id: str | None = None
    fan_check: FanCheckResult | None = None
    balancing: AirNetworkBalancing = Field(default_factory=AirNetworkBalancing)
    solver: AirNetworkSolverStatus
    node_balances: list[AirNodeBalance] = Field(default_factory=list)
    floor_airflow_summary: dict[str, dict[str, float]] = {}
    issues: list[Issue] = []


class SteadyStateSimulationResult(BaseModel):
    supply_water_temp_c: float
    return_water_temp_c: float
    supply_air_temp_c: float
    estimated_room_temp_c: float
    cooling_energy_kw: float
    pump_energy_kw: float
    fan_energy_kw: float
    total_energy_kw: float
    issues: list[Issue] = []


class SteadyPortResult(BaseModel):
    owner_id: str
    owner_kind: Literal["device", "pipe"]
    port_index: int
    direction: Literal["supply", "return", "bidirectional"]
    medium: Literal["chilled_water", "cooling_water", "hot_water", "air"] | None
    diameter_mm: float | None = None
    floor_id: str | None = None
    is_valid: bool = True
    flow_m3_h_by_medium: dict[str, float] = {}


class SteadyEdgeResult(BaseModel):
    edge_id: str
    medium: Literal["chilled_water", "cooling_water", "hot_water", "air"]
    from_owner_id: str
    from_port_index: int
    to_owner_id: str
    to_port_index: int
    pipe_id: str | None = None
    length_m: float = 0.0
    diameter_mm: float | None = None
    flow_m3_h: float = 0.0


class SteadyManifoldResult(BaseModel):
    manifold_id: str
    main_flow_m3_h: float
    branch_flows_m3_h: dict[str, float]
    residual_m3_h: float
    port_bindings: dict[str, int]


class SteadyMediumResult(BaseModel):
    total_demand_m3_h: float
    actual_flow_m3_h: float = 0.0
    unmet_flow_m3_h: float = 0.0
    unmet_load_kw: float | None = None
    limiting_device_ids: list[str] = Field(default_factory=list)
    converged: bool
    max_conservation_residual_m3_h: float
    conservation_residuals_m3_h: dict[str, float]
    edges: list[SteadyEdgeResult]
    manifolds: list[SteadyManifoldResult] = Field(default_factory=list)


class EquipmentBalanceResult(BaseModel):
    cooling_load_kw: float
    heating_load_kw: float
    cold_source_capacity_kw: float
    heat_source_capacity_kw: float
    terminal_capacity_kw: float
    cooling_delivered_kw: float
    heating_delivered_kw: float
    cooling_balance_residual_kw: float
    heating_balance_residual_kw: float
    simulation: SteadyStateSimulationResult
    issues: list[Issue] = []


class SteadyNetworkResult(BaseModel):
    ports: list[SteadyPortResult]
    media: dict[str, SteadyMediumResult]
    hydraulic: HydraulicNetworkResult | None = None
    air: AirNetworkResult | None = None
    equipment: EquipmentBalanceResult | None = None
    summary: dict[str, Any]
    issues: list[Issue]


class EnergySimulationRequest(BaseModel):
    project: SystemEvaluateRequest
    hours: int = Field(default=24, ge=1, le=8760)
    occupancy_start: int = Field(default=8, ge=0, le=23)
    occupancy_end: int = Field(default=18, ge=1, le=24)
    chiller_cop: float = Field(default=4.5, gt=0)
    electricity_price_per_kwh: float = Field(default=0.85, ge=0)


class EnergyHourlyPoint(BaseModel):
    hour: int
    load_kw: float
    part_load_ratio: float
    cooling_energy_kw: float
    pump_energy_kw: float
    fan_energy_kw: float
    total_energy_kw: float


class EnergySimulationResult(BaseModel):
    hours: int
    peak_load_kw: float
    peak_energy_kw: float
    cooling_energy_kwh: float
    pump_energy_kwh: float
    fan_energy_kwh: float
    total_energy_kwh: float
    estimated_cost: float
    hourly: list[EnergyHourlyPoint]
    issues: list[Issue] = []


class RealtimeDeviceState(BaseModel):
    project_id: str
    device_id: str
    timestamp: str | None = None
    status: Literal["running", "stopped", "alarm", "offline"] = "running"
    values: dict[str, float | str | bool | None] = {}


class RealtimeUpdateRequest(BaseModel):
    project_id: str
    states: list[RealtimeDeviceState]


class RealtimeSystemState(BaseModel):
    project_id: str
    updated_at: str | None = None
    devices: dict[str, RealtimeDeviceState] = {}
    alarms: list[Issue] = []
    summary: dict[str, Any] = {}
    last_result: dict[str, Any] | None = None
    history_count: int = 0


class RealtimeHistoryResult(BaseModel):
    project_id: str
    points: list[dict[str, Any]]


class RealtimeSimulationControl(BaseModel):
    project_id: str
    running: bool
    interval_seconds: float = 1.0


class RealtimeRoomInitialCondition(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    room_id: str = Field(min_length=1)
    temperature_c: float = Field(ge=10.0, le=40.0)
    # 上限与 simcore 的 MAX_ROOM_RH_PCT 对齐：湿度改为动态状态后，
    # 前端"初始湿度"是用实测湿度回填的，实测可达 95，卡在 90 会让回填值无法保存。
    relative_humidity_pct: float = Field(ge=10.0, le=95.0)

    @field_validator("room_id")
    @classmethod
    def normalize_room_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("room_id must not be blank")
        return normalized


class RealtimeSimulationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    mode: Literal["cooling", "heating", "ventilation", "standby", "fault"] = "cooling"
    interval_seconds: float = Field(default=1.0, ge=0.2)
    initial_room_temp_c: float | None = Field(default=None, ge=10.0, le=40.0)
    room_initial_conditions: list[RealtimeRoomInitialCondition] = Field(
        default_factory=list
    )
    target_temp_c: float = Field(default=26.0, ge=10.0, le=40.0)
    target_humidity_pct: float = Field(default=55.0, ge=30.0, le=80.0)
    outdoor_base_temp_c: float = 33.0
    outdoor_relative_humidity_pct: float = Field(default=70.0, ge=5.0, le=99.0)
    fault_mode: (
        Literal[
            "none", "chiller_capacity_low", "pump_low_flow", "terminal_capacity_low"
        ]
        | None
    ) = "none"


class RealtimeRoomState(BaseModel):
    room_id: str
    name: str
    floor_id: str
    floor_name: str
    temperature_c: float
    target_temp_c: float
    relative_humidity_pct: float
    target_humidity_pct: float
    airflow_m3_h: float
    cooling_kw: float
    heating_kw: float


class RealtimeRoomStatesResult(BaseModel):
    project_id: str
    running: bool
    simcore_tick: int
    rooms: list[RealtimeRoomState]


class RealtimePipeState(BaseModel):
    pipe_id: str
    system: str
    pipe_type: str
    floor_id: str | None = None
    diameter_mm: float | None = None
    flow_m3_h: float
    velocity_m_s: float
    pressure_drop_pa: float
    head_m: float
    served_terminal_ids: list[str] = []


class RealtimePipeStatesResult(BaseModel):
    project_id: str
    running: bool
    simcore_tick: int
    pipes: list[RealtimePipeState]
    total_flow_by_system_m3_h: dict[str, float] = {}
    most_loaded_pipe_id: str | None = None


class RealtimeDeviceReading(BaseModel):
    project_id: str
    device_id: str
    device_type: str
    model_name: str | None = None
    floor_id: str | None = None
    floor_name: str | None = None
    role: str | None = None
    status: Literal["running", "stopped", "alarm", "offline"] = "running"
    parameter_origin: str | None = None
    parameters_degraded: bool = False
    control_source: str | None = None
    values: dict[str, float | str | bool | None] = {}


class RealtimeDeviceStatesResult(BaseModel):
    project_id: str
    running: bool
    simcore_tick: int
    devices: list[RealtimeDeviceReading]
    device_count: int = 0
    filtered_by_type: str | None = None


class RealtimeControlRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    device_instance_id: str = Field(min_length=1)
    values: dict[str, float]


class RealtimeControlsResetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    device_instance_id: str | None = None


class RealtimeControlValue(BaseModel):
    label: str
    kind: Literal["number", "switch"]
    unit: str
    min: float
    max: float
    step: float
    current: float


class RealtimeDeviceControls(BaseModel):
    model_name: str
    control_source: Literal["auto", "manual_override"] = "auto"
    controls: dict[str, RealtimeControlValue]


class RealtimeControlsResult(BaseModel):
    project_id: str
    devices: dict[str, RealtimeDeviceControls]


class RatedDeviceView(BaseModel):
    device_instance_id: str
    device_type: str
    model_name: str
    role: str
    quantity: int
    source: Literal["catalog", "type_default", "builtin_fallback"]
    degraded_fields: list[str]
    values: dict[str, float | bool | None]


class RatedDevicesResult(BaseModel):
    project_id: str
    devices: list[RatedDeviceView]


class DevicePortView(BaseModel):
    port_index: int
    direction: Literal["supply", "return", "bidirectional"]
    medium: Literal["chilled_water", "cooling_water", "hot_water", "air"] | None = None
    # 条件性端口（如 FCU/AHU 的热水口）：接了对应系统才有用，不接可空置。
    optional: bool = False


class DevicePortsResult(BaseModel):
    device_type: str
    model_name: str | None = None
    role: str
    # catalog=型号在设备库精确命中；type_default=按类型角色映射；inferred=按类型名推断
    source: Literal["catalog", "type_default", "inferred"]
    # 分集水器等动态端口设备：ports 只含固定主口，分支口由前端按连接数自增。
    dynamic_ports: bool = False
    ports: list[DevicePortView]


KnowledgeSystem = Literal[
    "chilled_water",
    "cooling_water",
    "hot_water",
    "air",
    "refrigerant",
    "source_water",
]


class EquipmentKnowledgeModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class EquipmentKnowledgeComponent(EquipmentKnowledgeModel):
    name: str = Field(min_length=1)
    function: str = Field(min_length=1)


class EquipmentKnowledgeParameter(EquipmentKnowledgeModel):
    key: str = Field(min_length=1)
    name: str = Field(min_length=1)
    unit: str | None
    description: str = Field(min_length=1)
    catalog_field: str | None


class EquipmentKnowledgeSummary(EquipmentKnowledgeModel):
    knowledge_id: str = Field(min_length=1)
    device_type: str = Field(min_length=1)
    name: str = Field(min_length=1)
    overview: str = Field(min_length=1)
    catalog_linked: bool
    catalog_device_type: str | None
    applicable_systems: list[KnowledgeSystem] = Field(min_length=1)


class EquipmentKnowledgeDetail(EquipmentKnowledgeSummary):
    principle: str = Field(min_length=1)
    components: list[EquipmentKnowledgeComponent] = Field(min_length=1)
    key_parameters: list[EquipmentKnowledgeParameter] = Field(min_length=1)


TeachingMetricDirection = Literal["increase", "decrease", "unchanged"]


class TeachingScenarioEffect(BaseModel):
    metric: str
    direction: TeachingMetricDirection


class TeachingScenarioControl(BaseModel):
    scope: Literal["device_control", "simulation_config"]
    target_role: str | None = None
    name: str
    value: float | Literal["installed_quantity"]


class TeachingScenarioDescriptor(BaseModel):
    id: str
    name: str
    description: str
    controls: list[TeachingScenarioControl]
    effects: list[TeachingScenarioEffect]


class TeachingComparisonRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scenario_id: str


class TeachingMetricComparison(BaseModel):
    before: float | None
    after: float | None
    change: float | None
    change_pct: float | None
    direction: TeachingMetricDirection | None
    unit: str
    applicable: bool


class TeachingConditionResult(BaseModel):
    metrics: dict[str, float | None]
    alarms: list[str]


class TeachingExplanation(BaseModel):
    code: str
    message: str
    related_devices: list[str]
    related_metrics: list[str]
    expected_direction: TeachingMetricDirection
    observed_direction: TeachingMetricDirection | None
    expectation_met: bool | None


class TeachingSteadyConvergence(BaseModel):
    converged: bool
    ticks: int
    final_room_drift_c: float


class TeachingComparisonConvergence(BaseModel):
    baseline: TeachingSteadyConvergence
    scenario: TeachingSteadyConvergence


class TeachingComparisonResult(BaseModel):
    project_id: str
    scenario: TeachingScenarioDescriptor
    baseline: TeachingConditionResult
    scenario_result: TeachingConditionResult
    metrics: dict[str, TeachingMetricComparison]
    explanations: list[TeachingExplanation]
    convergence: TeachingComparisonConvergence


SelectionTargetRole = Literal[
    "cold_source", "heat_source", "pump", "terminal"
]
SelectionStatus = Literal[
    "recommended", "no_eligible_candidates", "no_catalog_candidates"
]


class SelectionResponseModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class SelectionRecommendationRequest(SelectionResponseModel):
    target_role: SelectionTargetRole


class SelectionRequirements(SelectionResponseModel):
    cooling_load_kw: float = Field(ge=0)
    heating_load_kw: float = Field(ge=0)
    chilled_water_flow_m3_h: float = Field(ge=0)
    hot_water_flow_m3_h: float = Field(ge=0)
    required_pump_head_m: float = Field(ge=0)
    supply_airflow_m3_h: float = Field(ge=0)


class SelectionRuleRange(SelectionResponseModel):
    min: float = Field(gt=0)
    max: float = Field(gt=0)


class SelectionAppliedRules(SelectionResponseModel):
    capacity_margin: SelectionRuleRange
    pump_flow_margin: SelectionRuleRange
    pump_head_margin: SelectionRuleRange


class SelectionRatedValues(SelectionResponseModel):
    capacity_kw: float | None = Field(default=None, gt=0)
    chilled_water_flow_m3_h: float | None = Field(default=None, gt=0)
    flow_m3_h: float | None = Field(default=None, gt=0)
    head_m: float | None = Field(default=None, gt=0)
    airflow_m3_h: float | None = Field(default=None, gt=0)
    water_flow_m3_h: float | None = Field(default=None, gt=0)
    power_kw: float | None = Field(default=None, gt=0)
    cop: float | None = Field(default=None, gt=0)


class SelectionProvidedValues(SelectionResponseModel):
    capacity_kw: float | None = Field(default=None, gt=0)
    chilled_water_flow_m3_h: float | None = Field(default=None, gt=0)
    flow_m3_h: float | None = Field(default=None, gt=0)
    head_m: float | None = Field(default=None, gt=0)
    airflow_m3_h: float | None = Field(default=None, gt=0)
    water_flow_m3_h: float | None = Field(default=None, gt=0)
    power_kw: float | None = Field(default=None, gt=0)


class SelectionMargins(SelectionResponseModel):
    capacity: float | None = Field(default=None, ge=0)
    chilled_water_flow: float | None = Field(default=None, ge=0)
    hot_water_flow: float | None = Field(default=None, ge=0)
    flow: float | None = Field(default=None, ge=0)
    head: float | None = Field(default=None, ge=0)
    airflow: float | None = Field(default=None, ge=0)
    water_flow: float | None = Field(default=None, ge=0)


class SelectionReason(SelectionResponseModel):
    code: str
    message: str


class SelectionCandidate(SelectionResponseModel):
    rank: int = Field(ge=1)
    device_type: str
    model_name: str
    role: SelectionTargetRole
    quantity: int = Field(ge=1)
    rated: SelectionRatedValues
    provided: SelectionProvidedValues
    margins: SelectionMargins
    eligible: bool
    reasons: list[SelectionReason]


class SelectionRecommendationResult(SelectionResponseModel):
    project_id: str
    target_role: SelectionTargetRole
    status: SelectionStatus
    requirements: SelectionRequirements
    applied_rules: SelectionAppliedRules
    candidates: list[SelectionCandidate]


class ReportResult(BaseModel):
    summary: dict[str, Any]
    equipment: dict[str, Any]
    hydraulic: dict[str, Any] | None
    air: dict[str, Any] | None
    issues: list[Issue]
    recommendations: list[str]


class ProjectValidationResult(BaseModel):
    valid: bool
    normalized: SystemEvaluateRequest
    issues: list[Issue]
    schema_version: str = "1.0"


class CatalogImportRequest(BaseModel):
    filename: str
    content_base64: str
    replace: bool = False


class CatalogImportResult(BaseModel):
    imported: int
    total: int
    replaced: bool
    issues: list[Issue] = []


class ProjectSaveRequest(BaseModel):
    name: str
    project: SystemEvaluateRequest
    description: str | None = None
    tags: list[str] = []


class ProjectSummary(BaseModel):
    project_id: str
    name: str
    description: str | None = None
    tags: list[str] = []
    created_at: str
    updated_at: str
    history_count: int = 0


class ProjectRecord(ProjectSummary):
    project: SystemEvaluateRequest
    history: list[dict[str, Any]] = []


class ProjectHistoryEntry(BaseModel):
    history_id: str
    project_id: str
    created_at: str
    issue_count: int = 0
    summary: dict[str, Any] = {}
    result: dict[str, Any] | None = None


class SystemEvaluateResult(BaseModel):
    load: LoadResult | None
    water: WaterResult | None
    air: AirResult | None
    hydraulic_network: HydraulicNetworkResult | None = None
    air_network: AirNetworkResult | None = None
    topology: TopologyAnalysisResult | None = None
    simulation: SteadyStateSimulationResult | None = None
    steady_network: SteadyNetworkResult | None = None
    report: ReportResult | None = None
    issues: list[Issue]
    recommendations: list[str]
