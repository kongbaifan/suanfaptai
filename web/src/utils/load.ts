import type { RoomLoad } from "@/types";

/**
 * 冷负荷分项的固定顺序与中文名。
 *
 * 顺序与后端 `app/calculators/load.py` 的分项累计顺序一致：
 * 人员 → 照明 → 设备 → 新风显热 → 新风潜热 → 太阳辐射 → 围护传热。
 * 供暖两项单列，因为它们**不计入**冷负荷（后端的 `components_kw` 恒等于
 * `cooling_kw`，供暖分项单独成键）。
 *
 * 渲染必须按这个固定顺序，不能按对象键序——否则同一份结果在不同引擎里
 * 列的顺序会变。
 */
export const COMPONENT_LABELS: Record<string, string> = {
  people: "人员",
  lighting: "照明",
  equipment: "设备",
  fresh_air_cooling: "新风显热",
  latent_cooling: "新风潜热",
  solar: "太阳辐射",
  envelope_cooling: "围护传热(冷)",
  envelope_heating: "围护传热(热)",
  fresh_air_heating: "新风加热",
};

const COMPONENT_ORDER = [
  "people",
  "lighting",
  "equipment",
  "fresh_air_cooling",
  "latent_cooling",
  "solar",
  "envelope_cooling",
  "envelope_heating",
  "fresh_air_heating",
];

/** 结果里真正出现的分项键，按固定顺序返回。 */
export function componentKeys(rooms: RoomLoad[]): string[] {
  const keys = new Set(rooms.flatMap((room) => Object.keys(room.components_kw ?? {})));
  return COMPONENT_ORDER.filter((key) => keys.has(key));
}

/**
 * 逐房间分项求和。
 *
 * 冷负荷分项之和**恒等于**总冷负荷，这是后端保证的守恒关系
 * （见 `load.py` 对 `components_kw` 的注释）；本函数只是把它汇总到全楼，
 * 不做任何修正——若两者对不上，那是后端的问题，界面上应当看得见。
 */
export function sumComponents(rooms: RoomLoad[]): Record<string, number> {
  const totals: Record<string, number> = {};
  for (const room of rooms) {
    for (const [key, value] of Object.entries(room.components_kw ?? {})) {
      if (typeof value === "number" && Number.isFinite(value)) {
        totals[key] = (totals[key] ?? 0) + value;
      }
    }
  }
  return totals;
}

/** 冷负荷分项（用于判断正负与占比时的分组）。 */
export const COOLING_COMPONENT_KEYS = [
  "people",
  "lighting",
  "equipment",
  "fresh_air_cooling",
  "latent_cooling",
  "solar",
  "envelope_cooling",
];

export const ORIENTATION_OPTIONS = [
  { value: "south", label: "南" },
  { value: "north", label: "北" },
  { value: "east", label: "东" },
  { value: "west", label: "西" },
  { value: "internal", label: "内部（无外墙）" },
] as const;

export interface FieldSpec {
  key: string;
  label: string;
  unit: string;
  step: number;
  /** 后端 Pydantic 的校验下界（含）；不一致的值会得到 422。 */
  min?: number;
  max?: number;
  /** 输入框提示：这个字段在公式里起什么作用。 */
  hint: string;
}

/**
 * 全楼统一的设计参数。
 *
 * 后端把设计参数存在**每个房间**上（`RoomInput` 的字段），这里做成"一次改到
 * 全楼"是为了教学：课堂上不会逐房间改室外温度。若项目里各房间本来取值不同，
 * 界面会显示"各房间不一致"而不是假装统一（见 LoadView 的 `sharedValue`）。
 */
export const DESIGN_FIELDS: FieldSpec[] = [
  { key: "outdoor_summer_c", label: "夏季室外温度", unit: "°C", step: 0.5, hint: "决定新风显热与围护传热" },
  { key: "indoor_summer_c", label: "夏季室内温度", unit: "°C", step: 0.5, hint: "冷负荷的基准温度" },
  { key: "outdoor_winter_c", label: "冬季室外温度", unit: "°C", step: 0.5, hint: "决定供暖负荷" },
  { key: "indoor_winter_c", label: "冬季室内温度", unit: "°C", step: 0.5, hint: "供暖负荷的基准温度" },
  { key: "indoor_relative_humidity", label: "夏季室内相对湿度", unit: "", step: 0.05, min: 0.1, max: 0.9, hint: "与室外湿度之差决定新风潜热" },
  { key: "outdoor_relative_humidity", label: "夏季室外相对湿度", unit: "", step: 0.05, min: 0, max: 1, hint: "同上" },
];

/** 房间表里可直接编辑的字段。 */
export const ROOM_FIELDS: FieldSpec[] = [
  { key: "area_m2", label: "面积", unit: "m²", step: 1, min: 0.1, hint: "围护、太阳辐射、照明与设备负荷都按面积算" },
  { key: "people", label: "人数", unit: "人", step: 1, min: 0, hint: "决定人员负荷与卫生新风量" },
];

/** 高级参数：每房间可单独改，默认折叠。 */
export const ADVANCED_FIELDS: FieldSpec[] = [
  { key: "height_m", label: "层高", unit: "m", step: 0.1, min: 0.1, hint: "外墙面积 = √面积 × 层高" },
  { key: "window_wall_ratio", label: "窗墙比", unit: "", step: 0.05, min: 0, max: 0.9, hint: "占**外墙**面积的比例，不是地面面积" },
  { key: "envelope_u_w_m2k", label: "围护传热系数", unit: "W/(m²·K)", step: 0.1, min: 0.1, hint: "围护传热 = U × 外区面积 × 室内外温差" },
  { key: "exposed_area_factor", label: "外区面积系数", unit: "", step: 0.1, min: 0.1, hint: "参与传热的面积 = 房间面积 × 该系数" },
];

export function orientationLabel(value: unknown): string {
  const found = ORIENTATION_OPTIONS.find((option) => option.value === value);
  return found?.label ?? "南";
}
