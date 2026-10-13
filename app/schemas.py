from enum import Enum
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
    # Air outlets are topology boundaries, not conditioning terminals. Several
    # rooms may explicitly share an outlet (a teaching zone).
    supply_air_outlet_ids: list[str] = []
    return_air_outlet_ids: list[str] = []
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
    # 面积指标法的校核值，与 cooling_kw（分项法）并列，不参与求和。
    index_estimate_kw: float = 0.0
    # (分项法 - 指标法) / 指标法 × 100，供学生看两种口径的偏差。
    index_deviation_pct: float = 0.0
    # 供暖侧同构的指标校核值。
    heating_index_estimate_kw: float = 0.0
    heating_index_deviation_pct: float = 0.0
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


class LoadFactorBreakdown(BaseModel):
    """逐房间冷负荷表回显的系数：修正后 = 合计 × 同时使用系数 × 安全系数。"""

    simultaneity_factor: float
    safety_factor: float
    combined_factor: float


class RoomLoadBreakdownRow(BaseModel):
    """逐房间冷负荷表的一行。

    行类型决定哪些字段有值：`room` 有面积/人数/指标校核值，`floor_subtotal` 有
    面积/人数，`building_total` 只有全楼合计，`adjusted_total` 是逐分项乘系数后的口径
    （它的 `cooling_kw` 就是修正后合计）。小计/合计行的分项是各房间**已四舍五入的
    分项**之和，因此表里的数字可以直接相加核对。
    """

    row_type: Literal["room", "floor_subtotal", "building_total", "adjusted_total"]
    # 客户端选中行的稳定标识：房间用房间 id（无 id 时 `room_{序号}`），小计用楼层 id，
    # 合计用 `ALL`，修正后用 `ALL_ADJUSTED`。
    key: str
    name: str
    floor_id: str | None = None
    floor_name: str | None = None
    area_m2: float | None = None
    people: int | None = None
    # 冷负荷分项，键与顺序见 RoomLoadBreakdownResult.component_keys。
    components_kw: dict[str, float] = {}
    cooling_kw: float = 0.0
    # 合计 ÷ 面积，W/m²；面积缺失时为 null。
    cooling_index_w_m2: float | None = None
    # 合计 × 同时使用系数 × 安全系数（修正后口径）。
    adjusted_cooling_kw: float | None = None
    # 以下三项只有房间行有值，供"点一行看手算过程"取输入。
    fresh_air_m3_h: float | None = None
    index_estimate_kw: float | None = None
    index_deviation_pct: float | None = None


class RoomLoadBreakdownResult(BaseModel):
    """逐房间冷负荷表：房间行 → 楼层小计 → 全楼合计 → 修正后，按 `rows` 顺序渲染。

    口径与 `/api/v1/load/calculate` 完全一致（分项法，合计未乘系数），只是把结果摊平成表。
    """

    # 列顺序：人员 / 照明 / 设备 / 太阳辐射 / 围护 / 新风显热 / 新风潜热。
    component_keys: list[str]
    factors: LoadFactorBreakdown
    total_area_m2: float
    total_people: int
    rows: list[RoomLoadBreakdownRow]
    issues: list[Issue] = []


class RoomLoadOverride(BaseModel):
    """项目级表格接口的房间改动：只允许改面积与人数。

    `room_id` 与 `index` 至少给一个，两个都给时按 `index` 定位并校验 `room_id` 一致。
    """

    room_id: str | None = None
    index: int | None = Field(default=None, ge=0)
    area_m2: float | None = Field(default=None, gt=0)
    people: int | None = Field(default=None, ge=0)


class RoomLoadBreakdownOverrides(BaseModel):
    """项目级表格接口的请求体，整体可选：不给就用项目原值。

    改动只在本次计算里生效，不写回项目——要落库请用 `PUT /api/v1/projects/{project_id}`。
    """

    rooms: list[RoomLoadOverride] = []
    simultaneity_factor: float | None = Field(default=None, gt=0, le=1.0)
    safety_factor: float | None = Field(default=None, gt=0)


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
    air_connection_mode: Literal["room", "ducted"] | None = None
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


class SystemForm(str, Enum):
    """空调系统制式（空调系统形式）。

    制式是**项目级**声明：一个项目只采用一种制式，它决定末端设备族、
    新风由谁承担、以及拓扑应当满足的完整性条件。

    未声明（`None`）是合法状态，表示"只做通用校验"——存量项目与全部内置
    样例都未声明制式，行为与引入本字段之前完全一致。
    """

    FCU_FRESH_AIR = "fcu_fresh_air"  # 风机盘管 + 独立新风机组（温湿分控样板）
    ALL_AIR = "all_air"  # 全空气（组合式空调机组 + 风管树，本版本只做定风量）


class SystemEvaluateRequest(BaseModel):
    floors: list[FloorInput] = []
    rooms: list[RoomInput] = []
    devices: list[DeviceInput] = []
    pipes: list[PipeInput] = []
    connections: list[ConnectionInput] = []
    simultaneity_factor: float = Field(default=0.9, gt=0, le=1.0)
    safety_factor: float = Field(default=1.1, gt=0)
    design_options: dict[str, Any] = {}
    # 制式声明。默认 None = 未声明，只做制式无关的通用校验。
    system_form: SystemForm | None = None


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
    # 首尾相接的闭合环路（供程 + 回程都在内）。只有闭合环路才能用来定水泵扬程：
    # 半条路径缺回程阻力，据此选泵会偏小近一半。风系统是开式的，恒为 False。
    closed_circuit: bool = False


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


class RuntimeNetworkNode(BaseModel):
    node_id: str
    system: str
    circuit_id: str | None = None
    model_status: Literal["modeled", "disconnected", "unsupported"] = "modeled"
    flow_m3_h: float = 0.0
    velocity_m_s: float | None = None
    pressure_drop_pa: float | None = None
    head_m: float | None = None
    diameter_mm: float | None = None
    supply_temp_c: float | None = None
    return_temp_c: float | None = None
    flow_blocked: bool = False
    served_terminal_ids: list[str] = []
    parameter_sources: list[str] = []


class RuntimeCircuitState(BaseModel):
    circuit_id: str
    system: str
    source_ids: list[str] = []
    drive_ids: list[str] = []
    model_status: Literal["modeled", "disconnected", "unsupported"] = "modeled"
    flow_m3_h: float = 0.0
    supply_temp_c: float | None = None
    return_temp_c: float | None = None


class RealtimeNetworkResult(BaseModel):
    model: Literal["bounded_teaching_network"] = "bounded_teaching_network"
    pressure_balance_solved: bool = False
    circuits: list[RuntimeCircuitState] = []
    pipes: list[RuntimeNetworkNode] = []
    ducts: list[RuntimeNetworkNode] = []
    devices: list[RuntimeNetworkNode] = []
    issues: list[Issue] = []


class RealtimeSystemState(BaseModel):
    project_id: str
    updated_at: str | None = None
    devices: dict[str, RealtimeDeviceState] = {}
    alarms: list[Issue] = []
    summary: dict[str, Any] = {}
    last_result: dict[str, Any] | None = None
    history_count: int = 0
    session_id: str | None = None
    simcore_tick: int = 0
    applied_control_revision: int = 0
    network: RealtimeNetworkResult | None = None


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
    humidity_shortfall_pct: float = 0.0
    """湿度超出目标的百分点（不低于 0）。持续为正表示除湿能力不足，
    该房间的湿度目标在当前配置下不可达，需要再热或独立新风除湿。"""
    airflow_m3_h: float
    cooling_kw: float
    heating_kw: float


class RealtimeRoomStatesResult(BaseModel):
    session_id: str | None = None
    applied_control_revision: int = 0
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
    model_status: Literal["modeled", "disconnected", "unsupported"] = "modeled"
    flow_blocked: bool = False
    parameter_sources: list[str] = []


class RealtimePipeStatesResult(BaseModel):
    session_id: str | None = None
    applied_control_revision: int = 0
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
    session_id: str | None = None
    applied_control_revision: int = 0
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
    effective: float | None = None


class RealtimeDeviceControls(BaseModel):
    model_name: str
    control_source: Literal["auto", "manual_override"] = "auto"
    controls: dict[str, RealtimeControlValue]


class RealtimeControlsResult(BaseModel):
    project_id: str
    devices: dict[str, RealtimeDeviceControls]
    session_id: str | None = None
    control_revision: int = 0
    simcore_tick: int = 0
    applied_control_revision: int = 0


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


class SystemFormCheckResult(BaseModel):
    """制式自检结果。

    与 `ProjectValidationResult` 的分工：后者回答"这份项目数据格式对不对、
    水风系统自不自洽"，制式无关；本模型只回答"声明的系统制式在这个拓扑上
    成立吗"。两者都要看，制式自检不重复通用校验的内容。
    """

    system_form: SystemForm | None
    declared: bool
    compliant: bool
    issues: list[Issue]


class SystemGenerateOptions(BaseModel):
    """骨架生成的可调参数。全部有默认值，缺省即按规则库/计算器取值。"""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    # 是否同时生成热水环路与热源。`calculate_load` 会算出供暖负荷，没有热源时
    # 稳态容量平衡会如实报 energy_balance_violation（制热侧缺口）；内置样例都配了
    # 空气源热泵与热水环路，故默认开启。关掉它可用于只讲夏季工况的课堂。
    include_heating: bool = True


class GenerationDecision(BaseModel):
    """一条生成决策及其依据。

    "可解释性"是项目的既有纪律：每个结论都要说清是**目录额定值**、**规则库**
    还是**计算派生**得来的，不能是凭空写的数。
    """

    target: str
    choice: str
    reason: str
    source: Literal["catalog", "rules", "calculated", "default"]


class SystemGenerateRequest(BaseModel):
    """骨架生成请求：给建筑（楼层 + 房间）与制式，换回一套完整拓扑。

    `system_form` 在这里是**必填**——没有制式就不知道该生成哪一族系统。
    这与 `SystemEvaluateRequest.system_form` 可选（默认未声明）不同：评估可以
    离开制式独立进行，生成不行。
    """

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    floors: list[FloorInput] = []
    rooms: list[RoomInput] = []
    system_form: SystemForm
    simultaneity_factor: float = Field(default=0.9, gt=0, le=1.0)
    safety_factor: float = Field(default=1.1, gt=0)
    design_options: dict[str, Any] = {}
    options: SystemGenerateOptions = SystemGenerateOptions()


class SystemGenerateResult(BaseModel):
    """生成结果：一套可直接保存/评估的完整项目 + 决策记录 + 自检问题。

    `project` 已经过 `validate_project_data` 与制式自检；`issues` 是它们报出的
    问题。`degraded=True` 表示生成过程中有降级（例如某条需求在目录里没有合格
    型号，只能退到最大可用型号），此时 `issues` 里必有对应的 warning。
    """

    project: SystemEvaluateRequest
    decisions: list[GenerationDecision]
    issues: list[Issue]
    degraded: bool


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
