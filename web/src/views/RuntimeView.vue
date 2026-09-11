<script setup lang="ts">
import { computed, ref, watch } from "vue";
import {
  Activity,
  CircleStop,
  Droplets,
  Gauge,
  Play,
  RefreshCw,
  RotateCcw,
  Save,
  Thermometer,
  Waves,
} from "lucide-vue-next";
import StatusBadge from "@/components/StatusBadge.vue";
import { useProjectStore } from "@/stores/project";
import {
  copySimulationConfig,
  defaultSimulationConfig,
  useRealtimeStore,
} from "@/stores/realtime";
import type {
  DeviceInput,
  RealtimeDeviceState,
  RealtimePipeState,
  RoomInitialCondition,
  SimulationConfig,
} from "@/types";
import {
  dateTime,
  designParamLabel,
  finite,
  firstNumeric,
  paramLabel,
  runtimeFinite,
  valueWithUnit,
} from "@/utils/format";

const project = useProjectStore();
const realtime = useRealtimeStore();
const selectedId = ref<string | null>(null);
const typeFilter = ref("all");
const statusFilter = ref("all");
const configDraft = ref<SimulationConfig>(defaultSimulationConfig());
const initialDraft = ref<RoomInitialCondition[]>([]);
const controlDraft = ref<Record<string, number>>({});
const applyAllTemperature = ref(26);
const applyAllHumidity = ref(55);
const configSaved = ref(false);
const initialSaved = ref(false);
const controlsSaved = ref(false);

const deviceTypes = computed(() => [...new Set((project.project?.devices ?? []).map((item) => item.type))]);
const devices = computed(() => (project.project?.devices ?? []).filter((device) => {
  const state = realtime.current?.devices[device.id];
  return (typeFilter.value === "all" || device.type === typeFilter.value)
    && (statusFilter.value === "all" || state?.status === statusFilter.value);
}));
const selectedDevice = computed<DeviceInput | null>(() => project.project?.devices.find((item) => item.id === selectedId.value) ?? null);
const selectedState = computed<RealtimeDeviceState | null>(() => selectedId.value ? realtime.current?.devices[selectedId.value] ?? null : null);
const selectedControls = computed(() => selectedId.value ? realtime.controls?.devices[selectedId.value] ?? null : null);
const selectedRated = computed(() => selectedId.value ? realtime.rated?.devices.find((item) => item.device_instance_id === selectedId.value) ?? null : null);

type CardFieldSource = "design" | "rated" | "runtime";

interface CardFieldDefinition {
  label: string;
  source: CardFieldSource;
  key: string;
  unit?: string;
  fallbackRatedKey?: string;
  fallbackRuntimeKey?: string;
}

interface DeviceCardDefinition {
  label: string;
  rated: CardFieldDefinition[];
  runtime: CardFieldDefinition[];
}

interface DeviceCardField {
  label: string;
  unit: string;
  value: string;
}

interface DeviceCard {
  device: DeviceInput;
  label: string;
  rated: DeviceCardField[];
  runtime: DeviceCardField[];
  state: RealtimeDeviceState | null;
}

const DEVICE_CARD_DEFINITIONS: Record<string, DeviceCardDefinition> = {
  cold_source: {
    label: "冷水机组",
    rated: [
      { label: "额定制冷量", source: "design", key: "capacity_kw", fallbackRatedKey: "额定容量", unit: "kW" },
      { label: "额定 COP", source: "rated", key: "性能系数" },
      { label: "额定流量", source: "design", key: "flow_m3_h", fallbackRatedKey: "额定流量", unit: "m³/h" },
      { label: "额定功率", source: "design", key: "power_kw", fallbackRatedKey: "输入功率", unit: "kW" },
    ],
    runtime: [
      { label: "当前制冷量", source: "runtime", key: "cooling_kw", unit: "kW" },
      { label: "负荷率", source: "runtime", key: "load_ratio" },
      { label: "当前 COP", source: "runtime", key: "cop" },
      { label: "供水温度", source: "runtime", key: "supply_temp_c", unit: "°C" },
      { label: "回水温度", source: "runtime", key: "return_temp_c", unit: "°C" },
      { label: "当前流量", source: "runtime", key: "flow_m3_h", unit: "m³/h" },
    ],
  },
  cooling_tower: {
    label: "冷却塔",
    rated: [
      { label: "额定散热能力", source: "design", key: "capacity_kw", fallbackRatedKey: "额定容量", unit: "kW" },
      { label: "额定流量", source: "design", key: "flow_m3_h", fallbackRatedKey: "额定流量", unit: "m³/h" },
      { label: "风机功率", source: "rated", key: "风机功率", unit: "kW" },
      { label: "额定逼近度", source: "rated", key: "逼近度", unit: "°C" },
    ],
    runtime: [
      { label: "进塔水温", source: "runtime", key: "entering_water_temp_c", unit: "°C" },
      { label: "出塔水温", source: "runtime", key: "leaving_water_temp_c", unit: "°C" },
      { label: "当前流量", source: "runtime", key: "flow_m3_h", unit: "m³/h" },
      { label: "风机转速", source: "runtime", key: "fan_speed_pct", fallbackRuntimeKey: "fan_speed_ratio", unit: "%" },
      { label: "实际逼近度", source: "runtime", key: "approach_c", unit: "°C" },
    ],
  },
  pump: {
    label: "循环泵",
    rated: [
      { label: "额定流量", source: "design", key: "flow_m3_h", fallbackRatedKey: "额定流量", unit: "m³/h" },
      { label: "额定扬程", source: "design", key: "head_m", fallbackRatedKey: "额定扬程", unit: "m" },
      { label: "额定功率", source: "design", key: "power_kw", fallbackRatedKey: "输入功率", unit: "kW" },
    ],
    runtime: [
      { label: "当前流量", source: "runtime", key: "flow_m3_h", unit: "m³/h" },
      { label: "当前扬程", source: "runtime", key: "head_m", unit: "m" },
      { label: "转速", source: "runtime", key: "speed_pct", fallbackRuntimeKey: "speed_ratio", unit: "%" },
      { label: "当前功率", source: "runtime", key: "power_kw", unit: "kW" },
    ],
  },
  terminal: {
    label: "末端设备",
    rated: [
      { label: "额定制冷量", source: "design", key: "capacity_kw", fallbackRatedKey: "额定容量", unit: "kW" },
      { label: "额定风量", source: "rated", key: "额定风量", unit: "m³/h" },
      { label: "额定水量", source: "design", key: "flow_m3_h", fallbackRatedKey: "额定流量", unit: "m³/h" },
    ],
    runtime: [
      { label: "当前制冷量", source: "runtime", key: "cooling_kw", unit: "kW" },
      { label: "送风量", source: "runtime", key: "airflow_m3_h", unit: "m³/h" },
      { label: "送风温度", source: "runtime", key: "supply_air_temp_c", unit: "°C" },
      { label: "阀门开度", source: "runtime", key: "valve_opening_pct", fallbackRuntimeKey: "valve_opening_ratio", unit: "%" },
    ],
  },
  heat_source: {
    label: "热源机组",
    rated: [
      { label: "额定制热量", source: "design", key: "capacity_kw", fallbackRatedKey: "额定容量", unit: "kW" },
      { label: "额定 COP", source: "rated", key: "性能系数" },
      { label: "额定流量", source: "design", key: "flow_m3_h", fallbackRatedKey: "额定流量", unit: "m³/h" },
    ],
    runtime: [
      { label: "当前制热量", source: "runtime", key: "heating_kw", unit: "kW" },
      { label: "负荷率", source: "runtime", key: "load_ratio" },
      { label: "当前 COP", source: "runtime", key: "cop" },
      { label: "供水温度", source: "runtime", key: "supply_temp_c", unit: "°C" },
      { label: "回水温度", source: "runtime", key: "return_temp_c", unit: "°C" },
      { label: "当前流量", source: "runtime", key: "flow_m3_h", unit: "m³/h" },
    ],
  },
  heat_source_cooling: {
    label: "热泵机组（制冷）",
    rated: [
      { label: "额定制冷量", source: "design", key: "capacity_kw", fallbackRatedKey: "额定容量", unit: "kW" },
      { label: "额定流量", source: "design", key: "flow_m3_h", fallbackRatedKey: "额定流量", unit: "m³/h" },
    ],
    runtime: [
      { label: "当前制冷量", source: "runtime", key: "cooling_kw", unit: "kW" },
      { label: "负荷率", source: "runtime", key: "load_ratio" },
      { label: "制冷 COP", source: "runtime", key: "cop" },
      { label: "冷凝排热", source: "runtime", key: "heat_reject_kw", unit: "kW" },
      { label: "供水温度", source: "runtime", key: "supply_temp_c", unit: "°C" },
      { label: "回水温度", source: "runtime", key: "return_temp_c", unit: "°C" },
      { label: "当前流量", source: "runtime", key: "flow_m3_h", unit: "m³/h" },
    ],
  },
};

function ratedDevice(deviceId: string) {
  return realtime.rated?.devices.find((item) => item.device_instance_id === deviceId) ?? null;
}

function cardRole(device: DeviceInput): string | null {
  const stateRole = realtime.current?.devices[device.id]?.values.role;
  const ratedRole = ratedDevice(device.id)?.role;
  const role = String(stateRole ?? ratedRole ?? "");
  if (["cold_source", "chiller"].includes(role) || device.type === "冷源机组") return "cold_source";
  if (role === "cooling_tower" || device.type === "冷却塔") return "cooling_tower";
  if (["pump", "pump_chilled_water", "pump_cooling_water"].includes(role) || device.type === "循环泵") return "pump";
  if (["terminal", "fcu", "ahu", "fresh_air_unit"].includes(role) || ["风机盘管", "组合式空调机组", "一体式新风机组"].includes(device.type)) return "terminal";
  if (role === "heat_source" || ["空气源热泵", "水源热泵", "地源热泵机组"].includes(device.type)) {
    // 热泵冷热两用：制冷工况换向供冷，卡字段集随运行模式切换。
    return summaryMode.value === "cooling" ? "heat_source_cooling" : "heat_source";
  }
  return null;
}

function cardFieldValue(device: DeviceInput, definition: CardFieldDefinition): string {
  const rated = ratedDevice(device.id);
  const state = realtime.current?.devices[device.id];
  let value: unknown;
  let formatKey = definition.key;

  if (definition.source === "design") {
    value = device[definition.key as keyof DeviceInput];
    if ((value === null || value === undefined) && definition.fallbackRatedKey) {
      value = rated?.values[definition.fallbackRatedKey];
    }
  } else if (definition.source === "rated") {
    value = rated?.values[definition.key];
  } else {
    value = state?.values[definition.key];
    if ((value === null || value === undefined) && definition.fallbackRuntimeKey) {
      value = state?.values[definition.fallbackRuntimeKey];
      if (definition.fallbackRuntimeKey.endsWith("_ratio") && typeof value === "number") {
        value *= 100;
        formatKey = definition.key;
      }
    }
  }

  if (value === null || value === undefined) return "--";
  if (typeof value === "boolean") return value ? "是" : "否";
  if (typeof value === "number") {
    return definition.source === "runtime"
      ? runtimeFinite(formatKey, value)
      : finite(value, 3);
  }
  return String(value);
}

const deviceCards = computed<DeviceCard[]>(() => (project.project?.devices ?? []).flatMap((device) => {
  const role = cardRole(device);
  if (!role) return [];
  const definition = DEVICE_CARD_DEFINITIONS[role];
  const makeFields = (fields: CardFieldDefinition[]) => fields.map((field) => ({
    label: field.label,
    unit: field.unit ?? "",
    value: cardFieldValue(device, field),
  }));
  return [{
    device,
    label: definition.label,
    rated: makeFields(definition.rated),
    runtime: makeFields(definition.runtime),
    state: realtime.current?.devices[device.id] ?? null,
  }];
}));

const DESIGN_RATED_KEYS: Record<string, string> = {
  capacity_kw: "额定容量",
  flow_m3_h: "额定流量",
  head_m: "额定扬程",
  power_kw: "输入功率",
  pressure_drop_pa: "压降",
};

function ratedValueText(value: number | boolean | null): string {
  if (value === null || value === undefined) return "--";
  if (typeof value === "boolean") return value ? "是" : "否";
  return finite(value, 3);
}

function designValueText(key: string, value: unknown): unknown {
  if (value !== null && value !== undefined) return value;
  const ratedKey = DESIGN_RATED_KEYS[key];
  if (!ratedKey) return "--";
  return ratedValueText(selectedRated.value?.values[ratedKey] ?? null);
}

const RATED_SOURCE_LABELS: Record<string, string> = {
  catalog: "型号目录",
  type_default: "类型默认",
  builtin_fallback: "内置兜底",
};
const summary = computed(() => realtime.current?.summary ?? {});
/** 热泵卡双工况判定：运行模式（制冷时换向供冷）。 */
const summaryMode = computed(() => String(summary.value.mode ?? "cooling"));
const temperature = computed(() => firstNumeric(summary.value, ["average_room_temp_c", "room_temp_c", "supply_temp_c"]));
const humidity = computed(() => firstNumeric(summary.value, ["average_room_humidity_pct"]));
/** 湿度超出目标且持续存在的房间：除湿能力不足，目标在当前配置下不可达。 */
const humidityUnreachableRooms = computed(() =>
  (realtime.rooms?.rooms ?? []).filter(
    (room) => (room.humidity_shortfall_pct ?? 0) > 3.0,
  ),
);
const humidityTarget = computed(() => configDraft.value.target_humidity_pct);
const pressure = computed(() => firstNumeric(summary.value, ["pressure_pa", "total_pressure_pa", "pump_pressure_pa"]));
const flow = computed(() => firstNumeric(summary.value, ["total_flow_m3_h", "flow_m3_h", "chilled_water_flow_m3_h"]));
const load = computed(() => firstNumeric(
  summary.value,
  summary.value.mode === "heating"
    ? ["heating_load_kw", "system_load_kw"]
    : ["total_cooling_load_kw", "cooling_load_kw", "system_load_kw"],
));
const configValid = computed(() => [
  configDraft.value.interval_seconds,
  configDraft.value.target_temp_c,
  configDraft.value.target_humidity_pct,
  configDraft.value.outdoor_base_temp_c,
  configDraft.value.outdoor_relative_humidity_pct,
].every(Number.isFinite));
const initialValid = computed(() => initialDraft.value.every((item) => (
  Number.isFinite(item.temperature_c) && Number.isFinite(item.relative_humidity_pct)
)));
const controlValid = computed(() => Object.values(controlDraft.value).every(Number.isFinite));

// 水速上限，与后端 app/data/rules.json 的 water_velocity_m_s.max 一致。
// 只判上限：超限意味着噪音与冲刷腐蚀，任何工况下都是真问题。下限是设计工况的
// 选型校核口径（设计流量下流速过低说明管径选大了），实时读数处在部分负荷、
// 水泵降频后流速必然偏低，套用下限会把正常运行全标成异常。
const WATER_VELOCITY_MAX: Record<string, number> = {
  chilled_water: 2.5,
  cooling_water: 3.0,
  hot_water: 2.0,
};
const PIPE_SYSTEM_LABELS: Record<string, string> = {
  chilled_water: "冷冻水",
  cooling_water: "冷却水",
  hot_water: "热水",
};
const PIPE_TYPE_LABELS: Record<string, string> = {
  main_supply: "主供水",
  main_return: "主回水",
  branch_supply: "支供水",
  branch_return: "支回水",
  terminal_branch: "末端支管",
  riser_supply: "供水立管",
  riser_return: "回水立管",
  bypass: "旁通",
  unknown: "未标注",
};

const pipeSystemFilter = ref("all");
const pipeSystems = computed(() => [...new Set((realtime.pipes?.pipes ?? []).map((pipe) => pipe.system))]);
const pipes = computed(() => (realtime.pipes?.pipes ?? []).filter(
  (pipe) => pipeSystemFilter.value === "all" || pipe.system === pipeSystemFilter.value,
));
const flowingPipeCount = computed(() => (realtime.pipes?.pipes ?? []).filter((pipe) => pipe.flow_m3_h > 0).length);

function pipeSystemLabel(system: string): string {
  return PIPE_SYSTEM_LABELS[system] ?? system;
}

function pipeTypeLabel(pipeType: string): string {
  return PIPE_TYPE_LABELS[pipeType] ?? pipeType;
}

/** 无流量的管不判定流速：停机时全零是正常状态，不是超限。 */
function velocityTone(pipe: RealtimePipeState): "ok" | "warn" | "muted" {
  if (pipe.flow_m3_h <= 0) return "muted";
  const max = WATER_VELOCITY_MAX[pipe.system];
  return max !== undefined && pipe.velocity_m_s > max ? "warn" : "ok";
}

function velocityHint(pipe: RealtimePipeState): string {
  if (pipe.flow_m3_h <= 0) return "无流量";
  const max = WATER_VELOCITY_MAX[pipe.system];
  return max !== undefined && pipe.velocity_m_s > max ? `高于 ${max}` : "";
}

function resetInitialDraft() {
  const configured = new Map(realtime.config.room_initial_conditions.map((item) => [item.room_id, item]));
  initialDraft.value = (realtime.rooms?.rooms ?? []).map((room) => {
    const saved = configured.get(room.room_id);
    return {
      room_id: room.room_id,
      temperature_c: saved?.temperature_c ?? realtime.config.initial_room_temp_c ?? room.temperature_c,
      relative_humidity_pct: saved?.relative_humidity_pct ?? room.relative_humidity_pct,
    };
  });
  if (initialDraft.value[0]) {
    applyAllTemperature.value = initialDraft.value[0].temperature_c;
    applyAllHumidity.value = initialDraft.value[0].relative_humidity_pct;
  }
}

function resetControlDraft() {
  controlDraft.value = Object.fromEntries(
    Object.entries(selectedControls.value?.controls ?? {}).map(([name, control]) => [name, control.current]),
  );
}

async function saveBoundaryConfig() {
  configSaved.value = false;
  const accepted = await realtime.saveConfig(copySimulationConfig(configDraft.value));
  if (accepted) {
    configDraft.value = copySimulationConfig(accepted);
    configSaved.value = true;
  }
}

async function saveInitialConditions() {
  initialSaved.value = false;
  const accepted = await realtime.saveConfig({
    ...copySimulationConfig(configDraft.value),
    room_initial_conditions: initialDraft.value.map((condition) => ({ ...condition })),
  });
  if (accepted) {
    configDraft.value = copySimulationConfig(accepted);
    resetInitialDraft();
    initialSaved.value = true;
  }
}

function applyToAllRooms() {
  if (!Number.isFinite(applyAllTemperature.value) || !Number.isFinite(applyAllHumidity.value)) return;
  initialDraft.value = initialDraft.value.map((item) => ({
    ...item,
    temperature_c: applyAllTemperature.value,
    relative_humidity_pct: applyAllHumidity.value,
  }));
}

async function saveDeviceControls() {
  if (!selectedId.value) return;
  controlsSaved.value = false;
  if (await realtime.setDeviceControls(selectedId.value, { ...controlDraft.value })) {
    resetControlDraft();
    controlsSaved.value = true;
  }
}

async function resetSelectedControls() {
  if (selectedId.value && await realtime.resetControls(selectedId.value)) resetControlDraft();
}

async function resetAllControls() {
  if (await realtime.resetControls()) resetControlDraft();
}

async function startSimulation() {
  configSaved.value = false;
  await realtime.start(copySimulationConfig(configDraft.value));
}

watch(() => project.projectId, () => {
  selectedId.value = null;
  configSaved.value = false;
  initialSaved.value = false;
  controlsSaved.value = false;
});
watch(() => realtime.config, (value) => {
  configDraft.value = copySimulationConfig(value);
  resetInitialDraft();
}, { deep: true, immediate: true });
watch(() => realtime.rooms, resetInitialDraft, { deep: true });
watch([selectedId, () => realtime.controls], () => {
  controlsSaved.value = false;
  resetControlDraft();
}, { deep: true });
</script>

<template>
  <div class="view-stack">
    <section class="view-heading action-heading">
      <div><p class="eyebrow">REALTIME RUNTIME</p><h1>实时运行</h1><p>仿真控制、设备变量与当前告警</p></div>
      <div class="toolbar">
        <StatusBadge :tone="realtime.socketState === 'connected' ? 'ok' : 'warn'" :label="realtime.socketState" />
        <button class="icon-button" title="刷新运行数据" @click="realtime.syncRuntimeState"><RefreshCw /></button>
      </div>
    </section>

    <div v-if="project.usingSample" class="warning-banner">当前是未保存样例。先在顶部保存为项目，才能启动仿真和 WebSocket。</div>
    <div v-if="realtime.error" class="error-banner">{{ realtime.error }}</div>

    <section class="metric-grid live-metrics">
      <article class="metric-cell"><Thermometer /><span>室内温度</span><strong>{{ valueWithUnit(temperature, "°C") }}</strong></article>
      <article class="metric-cell"><Droplets /><span>室内湿度</span><strong>{{ valueWithUnit(humidity, "%", 1) }}</strong></article>
      <article class="metric-cell"><Gauge /><span>系统压力</span><strong>{{ valueWithUnit(pressure, "Pa", 0) }}</strong></article>
      <article class="metric-cell"><Waves /><span>循环流量</span><strong>{{ valueWithUnit(flow, "m³/h") }}</strong></article>
      <article class="metric-cell"><Activity /><span>系统负荷</span><strong>{{ valueWithUnit(load, "kW") }}</strong></article>
    </section>

    <div v-if="humidityUnreachableRooms.length" class="warning-banner humidity-diagnosis">
      湿度目标 {{ humidityTarget }}% 当前不可达（{{ humidityUnreachableRooms.length }} 个房间超标）：
      盘管除湿能力不足以处理人员散湿与新风湿负荷，继续降温只会让相对湿度更高。
      需配置再热盘管或独立新风除湿机组。
      <span class="humidity-diagnosis-rooms">{{ humidityUnreachableRooms.map((room) => `${room.name} ${room.relative_humidity_pct}%`).join("、") }}</span>
    </div>

    <section class="band-section equipment-overview">
      <div class="section-title">
        <div><p class="eyebrow">EQUIPMENT OVERVIEW</p><h2>设备数据展示</h2></div>
        <span>{{ deviceCards.length }} 台设备</span>
      </div>
      <div v-if="deviceCards.length" class="device-card-grid">
        <button
          v-for="card in deviceCards"
          :key="card.device.id"
          type="button"
          class="device-card"
          :class="{ selected: selectedId === card.device.id }"
          @click="selectedId = card.device.id"
        >
          <span class="device-card-header">
            <span class="device-card-title"><strong>{{ card.device.name || card.device.id }}</strong><small>{{ card.label }} · {{ card.device.type }}</small></span>
            <StatusBadge :tone="card.state?.status === 'running' ? 'ok' : card.state?.status === 'alarm' ? 'error' : 'muted'" :label="card.state?.status || '无实时数据'" />
          </span>
          <span class="device-card-meta"><span>{{ card.device.floor_id || '未分配楼层' }}</span><time>{{ dateTime(card.state?.timestamp) }}</time></span>
          <span class="device-card-group">
            <strong>设备参数</strong>
            <span v-for="field in card.rated" :key="`rated-${field.label}`" class="device-card-row"><span>{{ field.label }}</span><span>{{ field.value }}<small v-if="field.unit && field.value !== '--'">{{ field.unit }}</small></span></span>
          </span>
          <span class="device-card-group runtime-values">
            <strong>运行数据</strong>
            <span v-for="field in card.runtime" :key="`runtime-${field.label}`" class="device-card-row"><span>{{ field.label }}</span><span>{{ field.value }}<small v-if="field.unit && field.value !== '--'">{{ field.unit }}</small></span></span>
          </span>
        </button>
      </div>
      <div v-else class="empty-state">当前项目没有可展示的动态设备。</div>
    </section>

    <section class="band-section simulation-band">
      <div class="section-title">
        <div><p class="eyebrow">BOUNDARY & SYSTEM</p><h2>边界与系统条件</h2></div>
        <div class="title-actions">
          <StatusBadge :tone="realtime.running ? 'ok' : 'muted'" :label="realtime.running ? '运行中' : '已停止'" />
          <StatusBadge v-if="configSaved" tone="ok" label="已保存" />
        </div>
      </div>
      <div class="control-grid runtime-config-grid">
        <label>模式<select v-model="configDraft.mode" :disabled="realtime.running"><option value="cooling">制冷</option><option value="heating">制热</option><option value="ventilation">通风</option><option value="standby">待机</option><option value="fault">故障</option></select></label>
        <label>目标温度（°C）<input v-model.number="configDraft.target_temp_c" type="number" min="10" max="40" step="0.5" /></label>
        <label>目标湿度（%）<input v-model.number="configDraft.target_humidity_pct" type="number" min="30" max="80" step="1" /></label>
        <label>室外干球（°C）<input v-model.number="configDraft.outdoor_base_temp_c" type="number" step="0.5" /></label>
        <label>室外相对湿度（%）<input v-model.number="configDraft.outdoor_relative_humidity_pct" type="number" min="5" max="99" step="1" /></label>
        <label>刷新间隔（秒）<input v-model.number="configDraft.interval_seconds" type="number" min="0.2" step="0.2" /></label>
        <label>故障注入<select v-model="configDraft.fault_mode"><option value="none">无</option><option value="chiller_capacity_low">冷机容量不足</option><option value="pump_low_flow">水泵低流量</option><option value="terminal_capacity_low">末端容量不足</option></select></label>
      </div>
      <div class="runtime-command-row">
        <button class="button secondary" :disabled="project.usingSample || realtime.configSaving || !configValid" @click="saveBoundaryConfig"><Save />{{ realtime.configSaving ? '保存中' : '保存条件' }}</button>
        <button class="button primary" :disabled="project.usingSample || realtime.running || realtime.configSaving || !configValid" @click="startSimulation"><Play />启动</button>
        <button class="button danger" :disabled="project.usingSample || !realtime.running" @click="realtime.stop"><CircleStop />停止</button>
      </div>
    </section>

    <section class="band-section initial-state-band">
      <div class="section-title">
        <div><p class="eyebrow">INITIAL STATE</p><h2>初始状态</h2></div>
        <StatusBadge v-if="initialSaved" tone="ok" label="已保存" />
      </div>
      <fieldset :disabled="realtime.running || project.usingSample" class="initial-state-fieldset">
        <div class="apply-all-row">
          <label>全部温度（°C）<input v-model.number="applyAllTemperature" type="number" min="10" max="40" step="0.5" /></label>
          <label>全部湿度（%）<input v-model.number="applyAllHumidity" type="number" min="10" max="95" step="1" /></label>
          <button class="button secondary" type="button" @click="applyToAllRooms">应用到全部房间</button>
        </div>
        <div class="table-wrap runtime-room-table"><table><thead><tr><th>房间</th><th>楼层</th><th>初始温度（°C）</th><th>初始湿度（%）</th><th>当前温度（°C）</th><th>当前湿度（%）</th></tr></thead><tbody>
          <tr v-for="(condition, index) in initialDraft" :key="condition.room_id"><td><strong>{{ realtime.rooms?.rooms[index]?.name || condition.room_id }}</strong><small>{{ condition.room_id }}</small></td><td>{{ realtime.rooms?.rooms[index]?.floor_name || '--' }}</td><td><input v-model.number="condition.temperature_c" type="number" min="10" max="40" step="0.5" /></td><td><input v-model.number="condition.relative_humidity_pct" type="number" min="10" max="95" step="1" /></td><td>{{ runtimeFinite('temperature_c', realtime.rooms?.rooms[index]?.temperature_c) }}</td><td>{{ runtimeFinite('relative_humidity_pct', realtime.rooms?.rooms[index]?.relative_humidity_pct) }}</td></tr>
        </tbody></table></div>
        <div v-if="!initialDraft.length" class="empty-state">当前项目没有房间。</div>
        <div class="runtime-command-row"><button class="button secondary" :disabled="realtime.configSaving || !initialValid || !initialDraft.length" @click="saveInitialConditions"><Save />{{ realtime.configSaving ? '保存中' : '保存初始状态' }}</button></div>
      </fieldset>
    </section>

    <section class="runtime-layout">
      <div class="band-section device-browser">
        <div class="section-title"><div><p class="eyebrow">DEVICES</p><h2>设备状态</h2></div><div class="title-actions"><span>{{ devices.length }} 台</span><button class="button ghost" :disabled="realtime.controlsResetting" @click="resetAllControls"><RotateCcw />全部恢复自动</button></div></div>
        <div class="filter-row"><select v-model="typeFilter" aria-label="设备类型"><option value="all">全部类型</option><option v-for="type in deviceTypes" :key="type" :value="type">{{ type }}</option></select><select v-model="statusFilter" aria-label="设备状态"><option value="all">全部状态</option><option value="running">运行</option><option value="stopped">停止</option><option value="alarm">告警</option><option value="offline">离线</option></select></div>
        <div class="table-wrap"><table class="device-table"><thead><tr><th>设备</th><th>楼层</th><th>状态</th><th>更新时间</th></tr></thead><tbody>
          <tr v-for="device in devices" :key="device.id" :class="{ selected: selectedId === device.id }" @click="selectedId = device.id"><td><strong>{{ device.name || device.id }}</strong><small>{{ device.type }} · {{ device.id }}</small></td><td>{{ device.floor_id || '--' }}</td><td><StatusBadge :tone="realtime.current?.devices[device.id]?.status === 'running' ? 'ok' : realtime.current?.devices[device.id]?.status === 'alarm' ? 'error' : 'muted'" :label="realtime.current?.devices[device.id]?.status || '无数据'" /></td><td>{{ dateTime(realtime.current?.devices[device.id]?.timestamp) }}</td></tr>
        </tbody></table></div>
      </div>

      <aside class="band-section property-panel">
        <div class="section-title"><div><p class="eyebrow">PROPERTIES</p><h2>设备属性</h2></div></div>
        <div v-if="selectedDevice" class="property-content">
          <div class="device-identity"><strong>{{ selectedDevice.name || selectedDevice.id }}</strong><span>{{ selectedDevice.type }} · {{ selectedDevice.id }}</span></div>
          <div class="property-group device-controls"><div class="property-heading"><h3>设备控制</h3><StatusBadge v-if="selectedControls" :tone="selectedControls.control_source === 'manual_override' ? 'warn' : 'ok'" :label="selectedControls.control_source" /></div>
            <div v-if="selectedControls && Object.keys(selectedControls.controls).length" class="control-list">
              <label v-for="(control, name) in selectedControls.controls" :key="name" class="device-control-row">
                <span>{{ control.label }}<small v-if="control.unit">{{ control.unit }}</small></span>
                <input v-if="control.kind === 'switch'" v-model="controlDraft[name]" class="switch-input" type="checkbox" :true-value="control.max" :false-value="control.min" />
                <span v-else class="numeric-control"><input v-model.number="controlDraft[name]" type="range" :min="control.min" :max="control.max" :step="control.step" /><input v-model.number="controlDraft[name]" type="number" :min="control.min" :max="control.max" :step="control.step" /></span>
              </label>
              <div class="device-control-actions"><button class="button secondary" :disabled="realtime.controlSavingDevice === selectedId || !controlValid" @click="saveDeviceControls"><Save />{{ realtime.controlSavingDevice === selectedId ? '保存中' : '保存控制' }}</button><button class="icon-button" title="恢复此设备自动控制" :disabled="realtime.controlsResetting" @click="resetSelectedControls"><RotateCcw /></button><StatusBadge v-if="controlsSaved" tone="ok" label="已接受" /></div>
            </div>
            <div v-else class="empty-state">该设备没有可用控制项。</div>
          </div>
          <div class="property-group"><h3>设计属性</h3><dl><template v-for="(value, key) in selectedDevice" :key="key"><dt>{{ designParamLabel(key) }}</dt><dd>{{ designValueText(String(key), value) }}</dd></template></dl></div>
          <div class="property-group">
            <div class="property-heading"><h3>额定参数</h3><StatusBadge v-if="selectedRated" :tone="selectedRated.source === 'catalog' ? 'ok' : 'warn'" :label="RATED_SOURCE_LABELS[selectedRated.source] ?? selectedRated.source" /></div>
            <dl v-if="selectedRated"><template v-for="(value, key) in selectedRated.values" :key="key"><dt>{{ key }}</dt><dd>{{ ratedValueText(value) }}</dd></template></dl>
            <div v-else class="empty-state">额定参数不可用。</div>
          </div>
          <div class="property-group"><h3>运行变量</h3><dl v-if="selectedState && Object.keys(selectedState.values).length"><template v-for="(value, key) in selectedState.values" :key="key"><dt>{{ paramLabel(key, String(selectedState.values.role ?? '')) }}</dt><dd>{{ typeof value === 'number' ? runtimeFinite(String(key), value) : value ?? '--' }}</dd></template></dl>
            <div v-else class="diagnostic-empty"><StatusBadge tone="warn" label="未启动" /><strong>尚无运行变量</strong><p>该项目的实时仿真尚未启动或已停止。启动仿真后设备会开始上报运行值。</p></div>
          </div>
        </div>
        <div v-else class="empty-state">从设备列表选择一台设备查看属性。</div>
      </aside>
    </section>

    <section class="band-section">
      <div class="section-title">
        <div><p class="eyebrow">PIPES</p><h2>管段读数</h2></div>
        <div class="title-actions">
          <span>{{ flowingPipeCount }} / {{ realtime.pipes?.pipes.length ?? 0 }} 有流量</span>
        </div>
      </div>
      <div v-if="realtime.pipes?.pipes.length" class="pipe-content">
        <div class="pipe-total-row">
          <span v-for="(value, system) in realtime.pipes.total_flow_by_system_m3_h" :key="system" class="pipe-total">
            <small>{{ pipeSystemLabel(String(system)) }}</small>
            <strong>{{ finite(value, 3) }} m³/h</strong>
          </span>
        </div>
        <div class="filter-row">
          <select v-model="pipeSystemFilter" aria-label="管段系统">
            <option value="all">全部系统</option>
            <option v-for="system in pipeSystems" :key="system" :value="system">{{ pipeSystemLabel(system) }}</option>
          </select>
        </div>
        <div class="table-wrap"><table><thead><tr><th>管段</th><th>系统</th><th>楼层</th><th>管径 (mm)</th><th>流量 (m³/h)</th><th>流速 (m/s)</th><th>压降 (Pa)</th><th>服务末端</th></tr></thead><tbody>
          <tr v-for="pipe in pipes" :key="pipe.pipe_id" :class="{ 'pipe-hot': pipe.pipe_id === realtime.pipes.most_loaded_pipe_id }">
            <td><strong>{{ pipe.pipe_id }}</strong><small>{{ pipeTypeLabel(pipe.pipe_type) }}</small></td>
            <td>{{ pipeSystemLabel(pipe.system) }}</td>
            <td>{{ pipe.floor_id || '--' }}</td>
            <td>{{ pipe.diameter_mm === null ? '--' : finite(pipe.diameter_mm, 0) }}</td>
            <td>{{ finite(pipe.flow_m3_h, 3) }}</td>
            <td><StatusBadge :tone="velocityTone(pipe)" :label="finite(pipe.velocity_m_s, 3)" /><small v-if="velocityHint(pipe)">{{ velocityHint(pipe) }}</small></td>
            <td>{{ finite(pipe.pressure_drop_pa, 1) }}</td>
            <td>{{ pipe.served_terminal_ids.join('、') || '--' }}</td>
          </tr>
        </tbody></table></div>
        <p class="table-note">
          流量由末端实时水量沿项目连接方向累加，流速与压降按流量反算，不求解压力平衡。管径过小形成的
          压力壁垒不会体现为流量重分配，只表现为该管流速偏高。设计校核请用评估页的水力计算。
        </p>
      </div>
      <div v-else class="empty-state">当前项目没有水管，或管段读数尚未加载。</div>
    </section>

    <section class="band-section">
      <div class="section-title"><h2>实时告警</h2><span>{{ realtime.current?.alarms.length ?? 0 }} 条</span></div>
      <div v-if="realtime.current?.alarms.length" class="issue-list"><article v-for="issue in realtime.current.alarms" :key="`${issue.code}-${issue.target}`" class="issue-row" :class="`issue-${issue.level}`"><StatusBadge :tone="issue.level === 'error' ? 'error' : 'warn'" :label="issue.code" /><div><strong>{{ issue.message }}</strong><small>{{ issue.target || '系统' }}</small></div></article></div>
      <div v-else class="empty-state good">当前没有实时告警。</div>
    </section>
  </div>
</template>
