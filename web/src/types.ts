export type LoadState = "idle" | "loading" | "ready" | "error";
export type DeviceStatus = "running" | "stopped" | "alarm" | "offline";

export interface Issue {
  level: "error" | "warning" | "info";
  code: string;
  message: string;
  target?: string | null;
}

export interface FloorInput {
  id: string;
  name?: string | null;
  level: number;
  elevation_m: number;
  height_m: number;
}

export interface RoomInput {
  id?: string | null;
  floor_id?: string | null;
  name: string;
  /** 服务本房间的末端设备 id。绑定的权威方向是房间 → 设备。 */
  device_ids: string[];
  area_m2: number;
  people: number;
  /** 房间用途：对应后端 PURPOSE_DEFAULTS 的键，负荷指标按用途取预设。 */
  purpose?: string;
  [key: string]: unknown;
}

export interface DeviceInput {
  id: string;
  type: string;
  name?: string | null;
  floor_id?: string | null;
  location_type?: string | null;
  capacity_kw?: number | null;
  flow_m3_h?: number | null;
  head_m?: number | null;
  power_kw?: number | null;
  pressure_drop_pa?: number | null;
  quantity: number;
}

export interface PipeInput {
  id: string;
  system: "chilled_water" | "cooling_water" | "hot_water" | "air" | string;
  pipe_type?: string;
  floor_id?: string | null;
  from_floor_id?: string | null;
  to_floor_id?: string | null;
  length_m: number;
  flow_m3_h?: number | null;
  diameter_mm?: number | null;
  width_mm?: number | null;
  height_mm?: number | null;
  local_resistance_coefficient?: number | null;
}

export interface ConnectionInput {
  from_id: string;
  to_id: string;
  system: string;
}

export interface ProjectData {
  floors: FloorInput[];
  rooms: RoomInput[];
  devices: DeviceInput[];
  pipes: PipeInput[];
  connections: ConnectionInput[];
  simultaneity_factor: number;
  safety_factor: number;
  design_options: Record<string, unknown>;
}

export interface ProjectSummary {
  project_id: string;
  name: string;
  description?: string | null;
  tags: string[];
  created_at: string;
  updated_at: string;
  history_count: number;
}

export interface ProjectRecord extends ProjectSummary {
  project: ProjectData;
  history: Record<string, unknown>[];
}

export interface FloorLoad {
  floor_id: string;
  floor_name: string;
  level: number;
  area_m2: number;
  people: number;
  room_count: number;
  cooling_kw: number;
  heating_kw: number;
  adjusted_cooling_kw: number;
  adjusted_heating_kw: number;
}

export interface RoomLoad {
  id: string | null;
  floor_id: string;
  name: string;
  cooling_kw: number;
  heating_kw: number;
  fresh_air_m3_h: number;
  /** 面积指标法校核值，不计入 cooling_kw */
  index_estimate_kw: number;
  /** 分项法相对指标法的偏差百分比 */
  index_deviation_pct: number;
  /** 供暖面积指标校核值，不计入 heating_kw */
  heating_index_estimate_kw: number;
  /** 供暖分项法相对指标法的偏差百分比 */
  heating_index_deviation_pct: number;
  components_kw: Record<string, number>;
}

export interface LoadResult {
  total_cooling_kw: number;
  total_heating_kw: number;
  adjusted_cooling_kw: number;
  adjusted_heating_kw: number;
  rooms: RoomLoad[];
  floor_loads: FloorLoad[];
  issues: Issue[];
}

export interface TopologyResult {
  node_count: number;
  edge_count_by_system: Record<string, number>;
  open_nodes_by_system: Record<string, string[]>;
  loop_count_by_system: Record<string, number>;
  isolated_nodes: string[];
  paths: Array<{ system: string; name: string; nodes: string[]; floor_ids: string[]; complete: boolean }>;
  pipe_flow_m3_h: Record<string, number>;
  floor_summary: Record<string, Record<string, unknown>>;
  vertical_network: Record<string, unknown>;
  issues: Issue[];
}

export interface PipeCalculation {
  pipe_id: string;
  system: string;
  flow_m3_h: number;
  size: Record<string, unknown>;
  velocity_m_s: number;
  pressure_drop_pa: number;
  pump_head_m?: number | null;
}

export interface PumpCheckResult {
  required_flow_m3_h: number;
  installed_flow_m3_h: number;
  required_head_m: number;
  available_head_m: number;
  duty_pump_count: number;
  standby_pump_count: number;
  flow_margin?: number | null;
  head_margin?: number | null;
  status: "ok" | "warning" | "error";
}

export interface HydraulicNetworkResult {
  total_flow_by_system_m3_h: Record<string, number>;
  pipes: PipeCalculation[];
  most_unfavorable_pipe_id?: string | null;
  required_pump_head_m: number;
  pump_check?: PumpCheckResult | null;
  issues: Issue[];
}

export interface SteadyStateSimulationResult {
  supply_water_temp_c: number;
  return_water_temp_c: number;
  supply_air_temp_c: number;
  estimated_room_temp_c: number;
  cooling_energy_kw: number;
  pump_energy_kw: number;
  fan_energy_kw: number;
  total_energy_kw: number;
  issues: Issue[];
}

export interface EvaluationResult {
  load: LoadResult | null;
  water: { flow_m3_h: number; pump_head_m: number; pressure_drop_pa: number } | null;
  air: { airflow_m3_h: number } | null;
  topology: TopologyResult | null;
  hydraulic_network?: HydraulicNetworkResult | null;
  simulation: SteadyStateSimulationResult | null;
  issues: Issue[];
  recommendations: string[];
  [key: string]: unknown;
}

export interface EnergyResult {
  hours: number;
  peak_load_kw: number;
  peak_energy_kw: number;
  total_energy_kwh: number;
  estimated_cost: number;
  hourly: Array<{ hour: number; load_kw: number; part_load_ratio: number; total_energy_kw: number }>;
  issues: Issue[];
}

export interface HistoryEntry {
  history_id: string;
  project_id: string;
  created_at: string;
  issue_count: number;
  summary: Record<string, unknown>;
  result?: Record<string, unknown> | null;
}

export interface RealtimeDeviceState {
  project_id: string;
  device_id: string;
  timestamp?: string | null;
  status: DeviceStatus;
  values: Record<string, number | string | boolean | null>;
}

export interface RealtimeState {
  project_id: string;
  updated_at?: string | null;
  devices: Record<string, RealtimeDeviceState>;
  alarms: Issue[];
  summary: Record<string, unknown>;
  last_result?: Record<string, unknown> | null;
  history_count: number;
}

export interface SimulationConfig {
  mode: "cooling" | "heating" | "ventilation" | "standby" | "fault";
  interval_seconds: number;
  initial_room_temp_c: number | null;
  room_initial_conditions: RoomInitialCondition[];
  target_temp_c: number;
  target_humidity_pct: number;
  outdoor_base_temp_c: number;
  outdoor_relative_humidity_pct: number;
  fault_mode: "none" | "chiller_capacity_low" | "pump_low_flow" | "terminal_capacity_low" | null;
}

export interface RoomInitialCondition {
  room_id: string;
  temperature_c: number;
  relative_humidity_pct: number;
}

export interface RealtimeRoomState {
  room_id: string;
  name: string;
  floor_id: string;
  floor_name: string;
  temperature_c: number;
  target_temp_c: number;
  relative_humidity_pct: number;
  target_humidity_pct: number;
  /** 湿度超出目标的百分点（≥0）；持续为正说明除湿能力不足、目标不可达 */
  humidity_shortfall_pct?: number;
  airflow_m3_h: number;
  cooling_kw: number;
  heating_kw: number;
}

export interface RealtimeRoomStatesResult {
  project_id: string;
  running: boolean;
  simcore_tick: number;
  rooms: RealtimeRoomState[];
}

export interface RealtimePipeState {
  pipe_id: string;
  system: string;
  pipe_type: string;
  floor_id: string | null;
  diameter_mm: number | null;
  flow_m3_h: number;
  velocity_m_s: number;
  pressure_drop_pa: number;
  head_m: number;
  served_terminal_ids: string[];
}

export interface RealtimePipeStatesResult {
  project_id: string;
  running: boolean;
  simcore_tick: number;
  pipes: RealtimePipeState[];
  total_flow_by_system_m3_h: Record<string, number>;
  most_loaded_pipe_id: string | null;
}

export interface RealtimeControlValue {
  label: string;
  kind: "number" | "switch";
  unit: string;
  min: number;
  max: number;
  step: number;
  current: number;
}

export interface RealtimeDeviceControls {
  model_name: string;
  control_source: "auto" | "manual_override";
  controls: Record<string, RealtimeControlValue>;
}

export interface RealtimeControlsResult {
  project_id: string;
  devices: Record<string, RealtimeDeviceControls>;
}

export interface DiagnosticCheck {
  name: string;
  ok: boolean;
  required: boolean;
  detail: string;
}

export interface DiagnosticsService {
  product: string;
  app_version: string;
  project_schema_version: string;
  python_version: string;
  platform: string;
  frozen: boolean;
  uptime_seconds: number;
}

export interface DiagnosticsData {
  catalog_items: number;
  catalog_types: number;
  catalog_issues: number;
  knowledge_entries: number;
  stored_projects: number;
}

export interface DiagnosticsRealtimeProject {
  project_id: string;
  tick: number;
  running: boolean;
  subscribers: number;
  updated_at?: string | null;
}

export interface DiagnosticsRealtime {
  active_sessions: number;
  running_simulations: number;
  websocket_subscribers: number;
  projects: DiagnosticsRealtimeProject[];
}

export interface Diagnostics {
  status: "ok" | "error";
  service: DiagnosticsService;
  checks: DiagnosticCheck[];
  data: DiagnosticsData;
  realtime: DiagnosticsRealtime;
}

// 设备额定参数：后端 GET /api/v1/projects/{id}/devices/rated 返回，
// values 的 key 已是中文参数名（如 "额定容量"、"额定流量"）
export interface RatedDeviceView {
  device_instance_id: string;
  device_type: string;
  model_name: string;
  role: string;
  quantity: number;
  source: "catalog" | "type_default" | "builtin_fallback";
  degraded_fields: string[];
  values: Record<string, number | boolean | null>;
}

export interface RatedDevicesResult {
  project_id: string;
  devices: RatedDeviceView[];
}

// 设备目录条目：后端 GET /api/v1/catalog/equipment 以中文字段名返回
export interface CatalogItem {
  设备类型?: string;
  型号?: string;
  设备角色?: string;
  额定容量?: number;
  额定流量?: number;
  额定扬程?: number;
  输入功率?: number;
  适用系统?: string[];
  [key: string]: unknown;
}
