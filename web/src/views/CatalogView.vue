<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { BookOpen, Database, Layers3, RefreshCw, Search } from "lucide-vue-next";
import { api, errorMessage } from "@/api/client";
import type { CatalogItem } from "@/types";

interface CatalogCard {
  type: string;
  role: string;
  items: CatalogItem[];
}

interface CatalogGroup {
  id: string;
  title: string;
  description: string;
  cards: CatalogCard[];
}

const GROUPS = [
  { id: "source", title: "冷热源与输配系统", description: "主机房设备", roles: new Set(["cold_source", "heat_source", "cooling_tower", "pump"]) },
  { id: "air", title: "空气处理与末端", description: "空调区域设备", roles: new Set(["terminal"]) },
] as const;

const DISPLAY_ROLES = new Set(GROUPS.flatMap((group) => [...group.roles]));

const ROLE_LABELS: Record<string, string> = {
  cold_source: "冷源设备",
  heat_source: "热源设备",
  cooling_tower: "冷却塔",
  pump: "水泵",
  terminal: "空气末端",
  air_terminal: "送风末端",
  pipe: "水管",
  duct: "风管",
  water_accessory: "水系统附件",
  air_accessory: "风系统附件",
};

const FIELD_ORDER = [
  "额定容量", "额定制热量", "额定制冷量", "额定流量", "源侧流量", "额定扬程", "输入功率", "制热输入功率", "制冷输入功率", "性能系数", "EER",
  "额定风量", "冷冻水流量", "冷却水流量", "冷冻水压降", "冷却水压降", "水流量", "水侧压降",
  "最小风量", "最大风量", "系列风量范围", "风机功率", "机外静压", "逼近度", "噪声", "噪声范围", "效率", "电机效率", "转速", "必需汽蚀余量", "局部阻力系数",
  "风侧压降", "推荐风量", "名义冷吨", "单价", "重量", "制冷剂", "按项目选型", "水冷", "变频能力",
];

const FIELD_UNITS: Record<string, string> = {
  额定容量: "kW", 额定制冷量: "kW", 额定制热量: "kW", cooling_capacity_kw: "kW", heating_capacity_kw: "kW",
  额定流量: "m³/h", 额定扬程: "mH₂O", 输入功率: "kW", 制冷输入功率: "kW", 制热输入功率: "kW", cooling_power_kw: "kW", heating_power_kw: "kW", 额定风量: "m³/h",
  冷冻水流量: "m³/h", 冷却水流量: "m³/h", 水流量: "m³/h", 源侧流量: "m³/h", 最小风量: "m³/h", 最大风量: "m³/h", 冷冻水压降: "kPa", 冷却水压降: "kPa",
  水侧压降: "kPa", 风机功率: "kW", 机外静压: "Pa", 噪声: "dB(A)", 转速: "r/min", 必需汽蚀余量: "m",
  风侧压降: "Pa", 推荐风量: "m³/h", 名义冷吨: "ton", 单价: "元", 重量: "kg", 制冷室外干球温度: "℃", 制热室外干球温度: "℃",
  cooling_outdoor_dry_bulb_temp_c: "℃", heating_outdoor_dry_bulb_temp_c: "℃",
};

const HEAT_PUMP_MODE_KEYS = new Set([
  "额定容量", "输入功率", "性能系数", "额定制冷量", "额定制热量", "制冷输入功率", "制热输入功率", "EER", "COP",
  "cooling_capacity_kw", "heating_capacity_kw", "cooling_power_kw", "heating_power_kw",
  "制冷蒸发器进水温度", "制冷蒸发器出水温度", "制冷冷凝器进水温度", "制冷冷凝器出水温度",
  "制热蒸发器进水温度", "制热蒸发器出水温度", "制热冷凝器进水温度", "制热冷凝器出水温度",
  "cooling_evaporator_inlet_temp_c", "cooling_evaporator_outlet_temp_c", "cooling_condenser_inlet_temp_c", "cooling_condenser_outlet_temp_c",
  "heating_evaporator_inlet_temp_c", "heating_evaporator_outlet_temp_c", "heating_condenser_inlet_temp_c", "heating_condenser_outlet_temp_c",
  "制冷室外干球温度", "制热室外干球温度", "cooling_outdoor_dry_bulb_temp_c", "heating_outdoor_dry_bulb_temp_c",
]);

const items = ref<CatalogItem[]>([]);
const selectedModels = ref<Record<string, string>>({});
const query = ref("");
const groupId = ref("all");
const loading = ref(false);
const error = ref("");
const showPlacement = ref(false);
const syncing = ref(false);
const syncNotice = ref("");

/** Unity 摆放条目只有预制体/贴图信息，不进工程参数卡片，默认隐藏。 */
const placementItems = computed(() => items.value.filter((item) => item["仅摆放"] === true));

const placementCards = computed<CatalogCard[]>(() => {
  const byType = new Map<string, CatalogItem[]>();
  for (const item of placementItems.value) {
    const type = String(item["设备类型"] || "未分类设备");
    const list = byType.get(type) ?? [];
    list.push(item);
    byType.set(type, list);
  }
  return [...byType.entries()]
    .map(([type, typeItems]) => ({
      type,
      role: String(typeItems[0]["设备角色"] || "unknown"),
      items: [...typeItems].sort((left, right) => String(left["型号"] || "").localeCompare(String(right["型号"] || ""))),
    }))
    .sort((left, right) => left.type.localeCompare(right.type, "zh-CN"));
});

const cards = computed<CatalogCard[]>(() => {
  const byType = new Map<string, CatalogItem[]>();
  for (const item of items.value) {
    if (item["型号"] === "*") continue;
    if (item["仅摆放"] === true) continue;
    const type = String(item["设备类型"] || "未分类设备");
    const list = byType.get(type) ?? [];
    list.push(item);
    byType.set(type, list);
  }
  return [...byType.entries()]
    .map(([type, typeItems]) => ({
      type,
      role: String(typeItems[0]["设备角色"] || "unknown"),
      items: [...typeItems].sort((left, right) => String(left["型号"] || "").localeCompare(String(right["型号"] || ""))),
    }))
    .filter((card) => DISPLAY_ROLES.has(card.role))
    .sort((left, right) => left.type.localeCompare(right.type, "zh-CN"));
});

const groups = computed<CatalogGroup[]>(() => {
  const normalizedQuery = query.value.trim().toLowerCase();
  return GROUPS.map((group) => ({
    ...group,
    cards: cards.value.filter((card) => {
      if (!group.roles.has(card.role)) return false;
      if (!normalizedQuery) return true;
      const values = card.items.flatMap((item) => Object.values(item).map((value) => Array.isArray(value) ? value.join(" ") : String(value ?? "")));
      return `${card.type} ${ROLE_LABELS[card.role] || card.role} ${values.join(" ")}`.toLowerCase().includes(normalizedQuery);
    }),
  })).filter((group) => groupId.value === "all" || group.id === groupId.value).filter((group) => group.cards.length > 0);
});

const actualModelCount = computed(() => cards.value.reduce((count, card) => count + card.items.length, 0));
const displayedItemCount = computed(() => cards.value.reduce((count, card) => count + card.items.length, 0));
const configurableTypeCount = computed(() => cards.value.filter((card) => card.items.some((item) => item["按项目选型"] === true || item.configurable_series === true)).length);
const visibleCardCount = computed(() => groups.value.reduce((count, group) => count + group.cards.length, 0));

function modelValue(card: CatalogCard) {
  const selected = selectedModels.value[card.type];
  return card.items.find((item) => item["型号"] === selected) ?? card.items[0];
}

function setDefaultSelections() {
  for (const card of cards.value) {
    const current = selectedModels.value[card.type];
    if (!current || !card.items.some((item) => item["型号"] === current)) {
      selectedModels.value[card.type] = String(card.items[0]["型号"] || "*");
    }
  }
}

function parameterKeys(item: CatalogItem, role: string) {
  const metadata = new Set([
    "设备类型", "型号", "设备角色", "适用系统", "品牌", "系列", "兼容旧型号", "厂家资料", "派生字段",
    "type", "name", "role", "systems", "brand", "series", "aliases", "source_url", "derived_fields",
  ]);
  const keys = Object.keys(item).filter((key) => !metadata.has(key) && !(role === "heat_source" && HEAT_PUMP_MODE_KEYS.has(key)));
  return [...FIELD_ORDER.filter((key) => keys.includes(key)), ...keys.filter((key) => !FIELD_ORDER.includes(key)).sort((left, right) => left.localeCompare(right, "zh-CN"))];
}

function modeValue(item: CatalogItem, keys: string[]) {
  const key = keys.find((candidate) => item[candidate] !== undefined && item[candidate] !== null && item[candidate] !== "");
  return key ? displayValue(key, item[key]) : "--";
}

function temperaturePair(item: CatalogItem, inletKeys: string[], outletKeys: string[]) {
  const inletKey = inletKeys.find((key) => item[key] !== undefined && item[key] !== null && item[key] !== "");
  const outletKey = outletKeys.find((key) => item[key] !== undefined && item[key] !== null && item[key] !== "");
  if (!inletKey || !outletKey) return "--";
  return `${item[inletKey]} / ${item[outletKey]} ℃`;
}

function displayValue(key: string, value: unknown) {
  if (value === null || value === undefined || value === "") return "--";
  if (typeof value === "boolean") return value ? "是" : "否";
  if (Array.isArray(value)) return value.join(" / ");
  if (typeof value === "number" && Number.isFinite(value)) {
    const number = key === "效率" && Math.abs(value) <= 1 ? value * 100 : value;
    const text = number.toLocaleString("zh-CN", { maximumFractionDigits: 3 });
    return `${text}${FIELD_UNITS[key] ? ` ${FIELD_UNITS[key]}` : ""}`;
  }
  return String(value);
}

function roleLabel(role: string) {
  return ROLE_LABELS[role] || role;
}

async function load() {
  loading.value = true;
  error.value = "";
  try {
    items.value = await api<CatalogItem[]>("/api/v1/catalog/equipment");
    setDefaultSelections();
  } catch (reason) {
    error.value = errorMessage(reason);
  } finally {
    loading.value = false;
  }
}

async function syncUnityLibrary() {
  syncing.value = true;
  syncNotice.value = "";
  try {
    const summary = await api<{ import: { attached: number; created: number; total: number }; export: { count: number; changed: boolean } }>(
      "/api/v1/catalog/equipment/sync-unity",
      { method: "POST" },
    );
    await load();
    syncNotice.value = `已同步 Unity 设备库：主库吸收 ${summary.import.attached + summary.import.created} 条（新增摆放条目 ${summary.import.created} 条），导出 ${summary.export.count} 条。`;
  } catch (reason) {
    syncNotice.value = errorMessage(reason);
  } finally {
    syncing.value = false;
  }
}

onMounted(load);
</script>

<template>
  <div class="view-stack catalog-view">
    <section class="view-heading action-heading">
      <div>
        <p class="eyebrow">EQUIPMENT LIBRARY</p>
        <h1>设备参数库</h1>
        <p>后端设备额定参数与设计选型数据</p>
      </div>
      <button class="icon-button" type="button" title="从 Unity DeviceLibrary.json 同步摆放件，并回写该文件" aria-label="同步 Unity 设备库" :disabled="syncing" @click="syncUnityLibrary"><RefreshCw :class="{ spin: syncing }" /></button>
      <button class="icon-button" type="button" title="刷新设备目录" aria-label="刷新设备目录" :disabled="loading" @click="load"><Database /></button>
    </section>

    <div v-if="error" class="error-banner">{{ error }}</div>
    <div v-if="syncNotice" class="notice-banner">{{ syncNotice }}</div>

    <section class="metric-grid">
      <article class="metric-cell"><Layers3 /><span>设备类型</span><strong>{{ cards.length || "--" }}</strong></article>
      <article class="metric-cell"><BookOpen /><span>明确型号</span><strong>{{ actualModelCount || "--" }}</strong></article>
      <article class="metric-cell"><Database /><span>目录记录</span><strong>{{ displayedItemCount || "--" }}</strong></article>
      <article class="metric-cell"><span>按项目选型</span><strong>{{ configurableTypeCount || "--" }}</strong></article>
    </section>

    <section class="band-section catalog-library">
      <div class="section-title">
        <div><h2>后端设备目录</h2><p class="catalog-section-note">选择型号查看对应的全部额定参数</p></div>
        <span>{{ visibleCardCount }} / {{ cards.length }} 类</span>
      </div>

      <div class="filter-row search-row catalog-filter-row">
        <label class="search-box"><Search /><input v-model="query" type="search" placeholder="搜索设备类型、型号或参数" aria-label="搜索设备类型、型号或参数" /></label>
        <select v-model="groupId" aria-label="设备分组"><option value="all">全部分组</option><option v-for="group in GROUPS" :key="group.id" :value="group.id">{{ group.title }}</option></select>
        <label class="catalog-placement-toggle"><input v-model="showPlacement" type="checkbox" />显示 Unity 摆放件（{{ placementItems.length }}）</label>
      </div>

      <section v-if="showPlacement && placementCards.length > 0" class="catalog-group">
        <header class="catalog-group-heading"><h3>Unity 摆放件</h3><span>仅承载 Unity 预制体/贴图信息，工程参数在 DeviceLibrary.json 同步时并入库</span></header>
        <div class="catalog-card-grid">
          <article v-for="card in placementCards" :key="card.type" class="catalog-equipment-card">
            <header class="catalog-card-heading"><h4>{{ card.type }}</h4><span>{{ roleLabel(card.role) }}</span></header>
            <dl class="catalog-parameter-list">
              <div class="catalog-parameter-row">
                <dt>摆放型号</dt>
                <dd class="catalog-model-names">{{ card.items.map((item) => item["型号"]).join("、") }}</dd>
              </div>
            </dl>
          </article>
        </div>
      </section>

      <div v-if="groups.length === 0" class="empty-state">暂无设备目录数据。</div>

      <section v-for="group in groups" :key="group.id" class="catalog-group">
        <header class="catalog-group-heading"><h3>{{ group.title }}</h3><span>{{ group.description }}</span></header>
        <div class="catalog-card-grid">
          <article v-for="card in group.cards" :key="card.type" class="catalog-equipment-card">
            <header class="catalog-card-heading"><h4>{{ card.type }}</h4><span>{{ roleLabel(card.role) }}</span></header>
            <dl class="catalog-parameter-list">
              <div class="catalog-parameter-row">
                <dt>设备型号</dt>
                <dd><select v-model="selectedModels[card.type]" class="catalog-model-select" :aria-label="`${card.type}设备型号`"><option v-for="item in card.items" :key="String(item['型号'])" :value="String(item['型号'])">{{ item["型号"] }}</option></select></dd>
              </div>
              <div v-if="card.role === 'heat_source'" class="catalog-operating-modes">
                <section class="catalog-mode">
                  <h5>制冷工况</h5>
                  <div class="catalog-mode-row"><dt>额定制冷量 (kW)</dt><dd>{{ modeValue(modelValue(card), ["额定制冷量", "cooling_capacity_kw"]) }}</dd></div>
                  <div class="catalog-mode-row"><dt>输入功率 (kW)</dt><dd>{{ modeValue(modelValue(card), ["制冷输入功率", "cooling_power_kw"]) }}</dd></div>
                  <div class="catalog-mode-row"><dt>EER</dt><dd>{{ modeValue(modelValue(card), ["EER", "eer"]) }}</dd></div>
                  <div class="catalog-mode-row"><dt>蒸发器进 / 出水 (℃)</dt><dd>{{ temperaturePair(modelValue(card), ["制冷蒸发器进水温度", "cooling_evaporator_inlet_temp_c"], ["制冷蒸发器出水温度", "cooling_evaporator_outlet_temp_c"]) }}</dd></div>
                  <div v-if="card.type === '空气源热泵'" class="catalog-mode-row"><dt>冷凝器空气侧室外干球温度 (℃)</dt><dd>{{ modeValue(modelValue(card), ["制冷室外干球温度", "cooling_outdoor_dry_bulb_temp_c"]) }}</dd></div>
                  <div v-else class="catalog-mode-row"><dt>冷凝器进 / 出水 (℃)</dt><dd>{{ temperaturePair(modelValue(card), ["制冷冷凝器进水温度", "cooling_condenser_inlet_temp_c"], ["制冷冷凝器出水温度", "cooling_condenser_outlet_temp_c"]) }}</dd></div>
                </section>
                <section class="catalog-mode">
                  <h5>制热工况</h5>
                  <div class="catalog-mode-row"><dt>额定制热量 (kW)</dt><dd>{{ modeValue(modelValue(card), ["额定制热量", "heating_capacity_kw", "额定容量"]) }}</dd></div>
                  <div class="catalog-mode-row"><dt>输入功率 (kW)</dt><dd>{{ modeValue(modelValue(card), ["制热输入功率", "heating_power_kw", "输入功率"]) }}</dd></div>
                  <div class="catalog-mode-row"><dt>COP</dt><dd>{{ modeValue(modelValue(card), ["COP", "性能系数"]) }}</dd></div>
                  <div v-if="card.type === '空气源热泵'" class="catalog-mode-row"><dt>蒸发器空气侧室外干球温度 (℃)</dt><dd>{{ modeValue(modelValue(card), ["制热室外干球温度", "heating_outdoor_dry_bulb_temp_c"]) }}</dd></div>
                  <div v-else class="catalog-mode-row"><dt>蒸发器进 / 出水 (℃)</dt><dd>{{ temperaturePair(modelValue(card), ["制热蒸发器进水温度", "heating_evaporator_inlet_temp_c"], ["制热蒸发器出水温度", "heating_evaporator_outlet_temp_c"]) }}</dd></div>
                  <div class="catalog-mode-row"><dt>冷凝器进 / 出水 (℃)</dt><dd>{{ temperaturePair(modelValue(card), ["制热冷凝器进水温度", "heating_condenser_inlet_temp_c"], ["制热冷凝器出水温度", "heating_condenser_outlet_temp_c"]) }}</dd></div>
                </section>
              </div>
              <div v-for="key in parameterKeys(modelValue(card), card.role)" :key="key" class="catalog-parameter-row"><dt>{{ key }}</dt><dd>{{ displayValue(key, modelValue(card)[key]) }}</dd></div>
            </dl>
          </article>
        </div>
      </section>
    </section>
  </div>
</template>

<style scoped>
.catalog-section-note { margin-top: 5px; color: var(--muted); font-size: 11px; font-weight: 500; }
.catalog-filter-row { padding-bottom: 20px; border-bottom: 1px solid var(--line); }
.catalog-group + .catalog-group { margin-top: 28px; }
.catalog-group-heading { display: flex; align-items: baseline; gap: 12px; margin-bottom: 12px; }
.catalog-group-heading h3 { color: var(--ink); font-size: 15px; }
.catalog-group-heading span { color: var(--muted); font-size: 12px; }
.catalog-card-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 14px; }
.catalog-equipment-card { position: relative; display: flex; min-width: 0; min-height: 390px; flex-direction: column; padding: 0 20px 18px; color: #eaf8f8; background: #123f4a; border: 1px solid #3c8990; border-radius: 3px; box-shadow: 0 6px 18px rgba(18, 63, 74, 0.1); }
.catalog-equipment-card::before, .catalog-equipment-card::after { position: absolute; top: -1px; width: 8px; height: 8px; border-top: 2px solid #5fd0cb; content: ""; }
.catalog-equipment-card::before { left: -1px; border-left: 2px solid #5fd0cb; }
.catalog-equipment-card::after { right: -1px; border-right: 2px solid #5fd0cb; }
.catalog-card-heading { display: flex; align-items: center; justify-content: space-between; gap: 10px; min-height: 54px; margin: 0 -20px 14px; padding: 0 16px; background: #1a5261; border-bottom: 1px solid rgba(160, 230, 230, 0.24); }
.catalog-card-heading h4 { min-width: 0; color: #fff; font-size: 15px; overflow-wrap: anywhere; }
.catalog-card-heading span { flex: 0 0 auto; color: #9dc7ca; font-size: 10px; }
.catalog-parameter-list { display: grid; gap: 0; margin: 0; }
.catalog-parameter-row { display: grid; grid-template-columns: minmax(0, 1.25fr) minmax(100px, 0.9fr); align-items: center; gap: 10px; min-height: 41px; border-bottom: 1px solid rgba(159, 220, 223, 0.12); }
.catalog-parameter-row dt { min-width: 0; color: #b8d3d4; font-size: 11px; line-height: 1.45; }
.catalog-parameter-row dd { min-width: 0; margin: 0; color: #67dde0; font-family: "Bahnschrift", "Microsoft YaHei", sans-serif; font-size: 12px; font-variant-numeric: tabular-nums; text-align: right; overflow-wrap: anywhere; }
.catalog-model-select { width: 100%; min-height: 32px; padding: 0 8px; color: #f0ffff; background: #167483; border: 1px solid rgba(111, 226, 224, 0.35); border-radius: 2px; }
.catalog-operating-modes { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; margin: 8px 0 4px; }
.catalog-mode { min-width: 0; background: rgba(15, 76, 87, 0.72); }
.catalog-mode h5 { margin: 0; padding: 7px 6px; color: #d9ffff; border-bottom: 1px solid rgba(159, 220, 223, 0.18); text-align: center; font-size: 11px; }
.catalog-mode-row { display: grid; grid-template-columns: minmax(0, 1fr) auto; align-items: center; gap: 6px; min-height: 35px; padding: 4px 7px; border-bottom: 1px solid rgba(159, 220, 223, 0.08); }
.catalog-mode-row:last-child { border-bottom: 0; }
.catalog-mode-row dt { min-width: 0; color: #b8d3d4; font-size: 11px; line-height: 1.45; }
.catalog-mode-row dd { min-width: 0; margin: 0; color: #67dde0; font-family: "Bahnschrift", "Microsoft YaHei", sans-serif; font-size: 12px; font-variant-numeric: tabular-nums; text-align: right; overflow-wrap: anywhere; }
@media (max-width: 1240px) { .catalog-card-grid { grid-template-columns: repeat(3, minmax(0, 1fr)); } }
@media (max-width: 1000px) { .catalog-card-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
@media (max-width: 620px) { .catalog-card-grid { grid-template-columns: 1fr; } .catalog-equipment-card { min-height: 0; } .catalog-group-heading { align-items: flex-start; flex-direction: column; gap: 3px; } .catalog-operating-modes { grid-template-columns: 1fr; } }
</style>
