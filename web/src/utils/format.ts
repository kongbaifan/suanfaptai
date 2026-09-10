export function finite(value: unknown, digits = 1): string {
  const numeric = typeof value === "number" ? value : typeof value === "string" && value.trim() ? Number(value) : Number.NaN;
  return Number.isFinite(numeric) ? numeric.toFixed(digits) : "--";
}

export function dateTime(value: unknown): string {
  if (typeof value !== "string" && typeof value !== "number") return "--";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "--" : date.toLocaleString("zh-CN", { hour12: false });
}

export function valueWithUnit(value: unknown, unit: string, digits = 1): string {
  const result = finite(value, digits);
  return result === "--" ? result : `${result} ${unit}`;
}

export function firstNumeric(record: Record<string, unknown> | undefined, keys: string[]): number | null {
  if (!record) return null;
  for (const key of keys) {
    const value = record[key];
    if (typeof value === "number" && Number.isFinite(value)) return value;
  }
  return null;
}

export function runtimeFinite(key: string, value: unknown): string {
  if (key === "enabled" || key === "active_count") return finite(value, 0);
  if (key.endsWith("_ratio") || key === "cop") return finite(value, 3);
  if (key.endsWith("_pct")) return finite(value, 1);
  if (key.endsWith("_temp_c") || key.endsWith("_kw") || key.endsWith("_m3_h")) {
    return finite(value, 3);
  }
  return finite(value, 2);
}

// 运行变量按设备角色区分的字段名（角色来自服务端 values.role）
const ROLE_PARAM_LABELS: Record<string, Record<string, string>> = {
  cold_source: {
    flow_m3_h: "冷冻水流量 (m³/h)",
  },
  heat_source: {
    flow_m3_h: "热水流量 (m³/h)",
  },
  pump: {
    flow_m3_h: "流量 (m³/h)",
    power_kw: "轴功率 (kW)",
  },
  cooling_tower: {
    flow_m3_h: "冷却水流量 (m³/h)",
  },
  terminal: {
    flow_m3_h: "水流量 (m³/h)",
    power_kw: "风机功率 (kW)",
  },
};

// 运行变量通用字段名（未按角色区分时兜底）
const PARAM_LABELS: Record<string, string> = {
  id: "设备ID",
  type: "设备类型",
  name: "名称",
  model: "型号",
  floor_id: "所在楼层",
  location_type: "安装位置",
  enabled: "启停",
  active_count: "运行台数",
  role: "设备角色",
  control_source: "控制来源",
  parameter_origin: "参数来源",
  parameters_degraded: "参数降级",
  status: "状态",
  load_ratio: "负荷率",
  cooling_kw: "制冷量 (kW)",
  heating_kw: "制热量 (kW)",
  cop: "COP",
  power_kw: "输入功率 (kW)",
  supply_temp_c: "供水温度 (°C)",
  return_temp_c: "回水温度 (°C)",
  heat_reject_kw: "冷凝排热 (kW)",
  speed_ratio: "转速比",
  speed_pct: "转速 (%)",
  head_m: "扬程 (m)",
  pressure_pa: "压力 (Pa)",
  fan_speed_ratio: "风机转速比",
  fan_speed_pct: "风机转速 (%)",
  approach_c: "逼近度 (°C)",
  entering_water_temp_c: "进塔水温 (°C)",
  leaving_water_temp_c: "出塔水温 (°C)",
  fan_power_kw: "风机功率 (kW)",
  valve_opening_ratio: "阀门开度比",
  valve_opening_pct: "阀门开度 (%)",
  airflow_m3_h: "送风量 (m³/h)",
  water_flow_m3_h: "水流量 (m³/h)",
  supply_air_temp_c: "送风温度 (°C)",
  opening_ratio: "开度比",
  resistance_ratio: "阻力倍率",
  flow_m3_h: "水流量 (m³/h)",
};

// 设备静态属性（设计属性）字段名
const DESIGN_PARAM_LABELS: Record<string, string> = {
  id: "设备ID",
  type: "设备类型",
  name: "名称",
  model: "型号",
  floor_id: "所在楼层",
  location_type: "安装位置",
  capacity_kw: "额定容量 (kW)",
  flow_m3_h: "额定流量 (m³/h)",
  head_m: "额定扬程 (m)",
  power_kw: "额定功率 (kW)",
  pressure_drop_pa: "压降 (Pa)",
  quantity: "台数",
  device_ids: "末端设备",
};

export function paramLabel(key: string, role?: string): string {
  const scoped = role ? ROLE_PARAM_LABELS[role]?.[key] : undefined;
  return scoped ?? PARAM_LABELS[key] ?? key;
}

export function designParamLabel(key: string): string {
  return DESIGN_PARAM_LABELS[key] ?? key;
}
