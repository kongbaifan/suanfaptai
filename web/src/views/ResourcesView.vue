<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from "vue";
import { BookOpen, CheckCircle2, RefreshCw, Search, SlidersHorizontal } from "lucide-vue-next";
import { api, errorMessage } from "@/api/client";
import { useProjectStore } from "@/stores/project";
import CatalogView from "./CatalogView.vue";
import type { EquipmentKnowledgeDetail, EquipmentKnowledgeSummary, SelectionResult, TeachingComparison, TeachingScenario } from "@/types/resources";

const projectStore = useProjectStore();
const tabs = [
  { id: "catalog", label: "参数目录" },
  { id: "knowledge", label: "设备知识" },
  { id: "selection", label: "设备选型" },
  { id: "teaching", label: "教学对比" },
] as const;
type Tab = (typeof tabs)[number]["id"];
const tab = ref<Tab>("catalog");
const error = ref("");
const notice = ref("");
const knowledge = ref<EquipmentKnowledgeSummary[]>([]);
const selectedKnowledge = ref<EquipmentKnowledgeDetail | null>(null);
const knowledgeQuery = ref("");
const knowledgeLoading = ref(false);
const selectionRole = ref<SelectionResult["target_role"]>("cold_source");
const selection = ref<SelectionResult | null>(null);
const selectionLoading = ref(false);
const scenarios = ref<TeachingScenario[]>([]);
const scenarioId = ref("");
const scenariosLoading = ref(false);
const comparison = ref<TeachingComparison | null>(null);
const teachingLoading = ref(false);
const knowledgeVersion = ref(0);
const selectionVersion = ref(0);
const teachingVersion = ref(0);
const detailPanel = ref<HTMLElement | null>(null);

const hasProject = computed(() => Boolean(projectStore.projectId && !projectStore.usingSample));
const filteredKnowledge = computed(() => {
  const q = knowledgeQuery.value.trim().toLowerCase();
  return q ? knowledge.value.filter((item) => `${item.name} ${item.device_type} ${item.overview}`.toLowerCase().includes(q)) : knowledge.value;
});
const roleLabels: Record<string, string> = { cold_source: "冷源", heat_source: "热源", pump: "水泵", terminal: "末端" };
const requirementLabels: Record<string, string> = { cooling_load_kw: "冷负荷", heating_load_kw: "热负荷", chilled_water_flow_m3_h: "冷冻水流量", hot_water_flow_m3_h: "热水流量", required_pump_head_m: "所需扬程", supply_airflow_m3_h: "送风量" };
const requirementUnits: Record<string, string> = { cooling_load_kw: "kW", heating_load_kw: "kW", chilled_water_flow_m3_h: "m³/h", hot_water_flow_m3_h: "m³/h", required_pump_head_m: "m", supply_airflow_m3_h: "m³/h" };
const metricLabels: Record<string, string> = { average_room_temp_c: "平均室温", total_power_kw: "系统总功率", chiller_power_kw: "冷机功率", total_flow_m3_h: "系统流量", delta_t_c: "供回水温差", condenser_water_temp_c: "冷却水温度", system_cop: "系统 COP" };
const format = (value: unknown, unit = "") => value === null || value === undefined ? "未提供" : typeof value === "number" ? `${value.toLocaleString("zh-CN", { maximumFractionDigits: 3 })}${unit ? ` ${unit}` : ""}` : String(value);
const requirementLabel = (key: string) => requirementLabels[key] || key;
const metricLabel = (key: string) => metricLabels[key] || key;
const marginText = (candidate: SelectionResult["candidates"][number]) => {
  const labels: Record<string, string> = { capacity: "容量", chilled_water_flow: "冷冻水流量", hot_water_flow: "热水流量", flow: "流量", head: "扬程", airflow: "风量", water_flow: "水流量" };
  return Object.entries(candidate.margins).filter(([, value]) => value !== null && value !== undefined).map(([key, value]) => `${labels[key] || key} ${format(value)}`).join("；") || "未提供";
};

async function loadKnowledge() {
  const version = ++knowledgeVersion.value;
  knowledgeLoading.value = true; error.value = "";
  try { const result = await api<EquipmentKnowledgeSummary[]>("/api/v1/knowledge/equipment"); if (version === knowledgeVersion.value) knowledge.value = result; }
  catch (reason) { if (version === knowledgeVersion.value) error.value = errorMessage(reason); } finally { if (version === knowledgeVersion.value) knowledgeLoading.value = false; }
}
async function openKnowledge(item: EquipmentKnowledgeSummary) {
  const version = ++knowledgeVersion.value; error.value = "";
  try { const detail = await api<EquipmentKnowledgeDetail>(`/api/v1/knowledge/equipment/${encodeURIComponent(item.knowledge_id)}`); if (version === knowledgeVersion.value) { selectedKnowledge.value = detail; await nextTick(); detailPanel.value?.scrollIntoView({ behavior: "smooth", block: "start" }); } }
  catch (reason) { if (version === knowledgeVersion.value) error.value = errorMessage(reason); }
}
async function recommend() {
  if (!hasProject.value) return;
  const projectKey = projectStore.projectId!; const version = ++selectionVersion.value;
  selectionLoading.value = true; error.value = "";
  try { const result = await api<SelectionResult>(`/api/v1/selection/recommend/${encodeURIComponent(projectKey)}`, { method: "POST", body: JSON.stringify({ target_role: selectionRole.value }) }); if (version === selectionVersion.value && projectStore.projectId === projectKey) selection.value = result; }
  catch (reason) { if (version === selectionVersion.value && projectStore.projectId === projectKey) error.value = errorMessage(reason); } finally { if (version === selectionVersion.value) selectionLoading.value = false; }
}
async function loadScenarios() {
  scenariosLoading.value = true;
  try { scenarios.value = await api<TeachingScenario[]>("/api/v1/teaching/scenarios"); if (!scenarioId.value) scenarioId.value = scenarios.value[0]?.id ?? ""; }
  catch (reason) { error.value = errorMessage(reason); } finally { scenariosLoading.value = false; }
}
async function compare() {
  if (!hasProject.value || !scenarioId.value) return;
  const projectKey = projectStore.projectId!; const version = ++teachingVersion.value;
  teachingLoading.value = true; error.value = "";
  try { const result = await api<TeachingComparison>(`/api/v1/teaching/compare/${encodeURIComponent(projectKey)}`, { method: "POST", body: JSON.stringify({ scenario_id: scenarioId.value }) }); if (version === teachingVersion.value && projectStore.projectId === projectKey) comparison.value = result; }
  catch (reason) { if (version === teachingVersion.value && projectStore.projectId === projectKey) error.value = errorMessage(reason); } finally { if (version === teachingVersion.value) teachingLoading.value = false; }
}
async function saveSample() { const saved = await projectStore.saveSample(); if (saved) notice.value = "样例已保存，现在可以使用设备选型和教学对比。"; }
watch(() => projectStore.selectionKey, () => { knowledgeVersion.value++; selectionVersion.value++; teachingVersion.value++; selectedKnowledge.value = null; selection.value = null; comparison.value = null; selectionLoading.value = false; teachingLoading.value = false; error.value = ""; });
onMounted(() => { loadKnowledge(); loadScenarios(); });
</script>

<template>
  <div class="view-stack resources-view">
    <section class="view-heading">
      <div><p class="eyebrow">LEARNING RESOURCES</p><h1>设备资源与教学</h1><p>从参数目录到设备原理、项目选型和工况对比，按学习任务逐步完成。</p></div>
      <div class="toolbar"><span class="notice-banner" v-if="projectStore.usingSample">当前为未保存样例</span><button v-if="projectStore.usingSample" class="button primary" :disabled="projectStore.saving" @click="saveSample">保存样例</button></div>
    </section>
    <div v-if="error" class="error-banner">{{ error }}</div><div v-if="projectStore.saveError" class="error-banner">保存失败：{{ projectStore.saveError }}</div><div v-if="notice" class="notice-banner">{{ notice }}</div>
    <nav class="workspace-tabs" role="tablist" aria-label="资源分区"><button v-for="item in tabs" :key="item.id" role="tab" :aria-selected="tab === item.id" :class="{ active: tab === item.id }" @click="tab = item.id">{{ item.label }}</button></nav>

    <CatalogView v-if="tab === 'catalog'" embedded />
    <template v-else-if="tab === 'knowledge'">
      <section class="band-section"><div class="section-title"><div><h2>设备知识</h2><p>查看后端维护的设备原理、组成和关键参数。</p></div><button class="button secondary" :disabled="knowledgeLoading" @click="loadKnowledge"><RefreshCw :class="{ spin: knowledgeLoading }" />刷新</button></div><label class="search-box"><Search /><input v-model="knowledgeQuery" placeholder="搜索设备名称或概述" /></label></section>
      <div class="resource-grid" v-if="filteredKnowledge.length"><article v-for="item in filteredKnowledge" :key="item.knowledge_id" class="resource-card" :class="{ selected: selectedKnowledge?.knowledge_id === item.knowledge_id }" role="button" tabindex="0" :aria-label="`查看${item.name}知识`" @click="openKnowledge(item)" @keydown.enter="openKnowledge(item)" @keydown.space.prevent="openKnowledge(item)"><div class="card-kicker">{{ item.device_type }} · {{ item.catalog_linked ? '已关联参数库' : '知识条目' }}</div><h3>{{ item.name }}</h3><p>{{ item.overview }}</p><div class="chip-row"><span v-for="system in item.applicable_systems" :key="system" class="chip">{{ system }}</span></div></article></div><div v-else class="empty-state">{{ knowledgeLoading ? '正在加载设备知识…' : '没有匹配的知识条目。' }}</div>
      <section v-if="selectedKnowledge" ref="detailPanel" class="band-section detail-panel"><div class="section-title"><div><h2>{{ selectedKnowledge.name }}</h2><p>{{ selectedKnowledge.principle }}</p></div></div><div class="resource-columns"><div><h3>组成与作用</h3><ul class="plain-list"><li v-for="part in selectedKnowledge.components" :key="part.name"><strong>{{ part.name }}</strong><span>{{ part.function }}</span></li></ul></div><div><h3>关键参数</h3><div class="table-wrap"><table><thead><tr><th>参数</th><th>单位</th><th>说明</th></tr></thead><tbody><tr v-for="parameter in selectedKnowledge.key_parameters" :key="parameter.key"><td>{{ parameter.name }}</td><td>{{ parameter.unit || '未提供' }}</td><td>{{ parameter.description }}</td></tr></tbody></table></div></div></div></section>
    </template>
    <template v-else-if="tab === 'selection'">
      <section class="band-section"><div class="section-title"><div><h2>设备选型</h2><p>输入目标设备，查看项目所需能力与候选型号。</p></div></div><div v-if="!hasProject" class="notice-banner">设备选型需要已保存项目。请先保存当前样例。</div><div class="toolbar"><label>目标设备<select v-model="selectionRole" :disabled="!hasProject"><option v-for="(label, role) in roleLabels" :key="role" :value="role">{{ label }}</option></select></label><button class="button primary" :disabled="!hasProject || selectionLoading" @click="recommend"><SlidersHorizontal />{{ selectionLoading ? '计算中…' : '开始推荐' }}</button></div></section>
      <section v-if="selection" class="band-section"><div class="metric-grid"><div v-for="(value, key) in selection.requirements" :key="key" class="metric-cell"><span>{{ requirementLabel(key) }}</span><strong>{{ format(value, requirementUnits[key]) }}</strong></div></div><div class="rule-note">容量余量 {{ selection.applied_rules.capacity_margin.min }}–{{ selection.applied_rules.capacity_margin.max }}；泵流量余量 {{ selection.applied_rules.pump_flow_margin.min }}–{{ selection.applied_rules.pump_flow_margin.max }}；泵扬程余量 {{ selection.applied_rules.pump_head_margin.min }}–{{ selection.applied_rules.pump_head_margin.max }}</div><p v-if="selection.status !== 'recommended'" class="notice-banner">{{ selection.status === 'no_eligible_candidates' ? '没有满足规则的候选设备。' : '目录中没有可用于该目标的候选设备。' }}</p><div class="table-wrap"><table><thead><tr><th>排名</th><th>型号</th><th>数量</th><th>资格</th><th>余量</th><th>原因</th></tr></thead><tbody><tr v-for="candidate in selection.candidates" :key="candidate.model_name"><td>{{ candidate.rank }}</td><td><strong>{{ candidate.model_name }}</strong><small>{{ candidate.device_type }}</small></td><td>{{ candidate.quantity }}</td><td><span :class="candidate.eligible ? 'status-good' : 'status-bad'">{{ candidate.eligible ? '可选' : '不满足' }}</span></td><td>{{ marginText(candidate) }}</td><td>{{ candidate.reasons.map((reason) => reason.message).join('；') || '—' }}</td></tr></tbody></table></div></section><div v-else-if="hasProject" class="empty-state">选择目标设备后开始推荐。</div>
    </template>
    <template v-else>
      <section class="band-section"><div class="section-title"><div><h2>教学对比</h2><p>选择一个场景，比较基线与调整后的指标变化。</p></div></div><div v-if="!hasProject" class="notice-banner">教学对比需要已保存项目。请先保存当前样例。</div><div v-if="scenariosLoading" class="table-note">正在加载教学场景…</div><div v-else-if="!scenarios.length" class="empty-state">暂无可用教学场景。</div><div class="toolbar"><label>教学场景<select v-model="scenarioId" :disabled="!hasProject || scenariosLoading"><option v-for="scenario in scenarios" :key="scenario.id" :value="scenario.id">{{ scenario.name }}</option></select></label><button class="button primary" :disabled="!hasProject || teachingLoading || !scenarioId" @click="compare">{{ teachingLoading ? '对比计算中…' : '运行对比' }}</button></div><p v-if="scenarios.find((scenario) => scenario.id === scenarioId)" class="table-note">{{ scenarios.find((scenario) => scenario.id === scenarioId)?.description }}</p></section>
      <section v-if="comparison" class="band-section"><div class="comparison-header"><h2>{{ comparison.scenario.name }}</h2><span :class="comparison.convergence.scenario.converged ? 'status-good' : 'status-bad'"><CheckCircle2 />{{ comparison.convergence.scenario.converged ? '已收敛' : '未收敛' }} · {{ comparison.convergence.scenario.ticks }} 步</span></div><div class="table-wrap"><table><thead><tr><th>指标</th><th>基线</th><th>场景后</th><th>变化</th><th>变化率</th><th>单位</th></tr></thead><tbody><tr v-for="(metric, name) in comparison.metrics" :key="name" v-show="metric.applicable"><td>{{ metricLabel(name) }}</td><td>{{ format(metric.before, metric.unit) }}</td><td>{{ format(metric.after, metric.unit) }}</td><td>{{ format(metric.change, metric.unit) }}</td><td>{{ metric.change_pct == null ? '未提供' : `${format(metric.change_pct)}%` }}</td><td>{{ metric.unit || '未提供' }}</td></tr></tbody></table></div><div class="explanation-list"><p v-for="explanation in comparison.explanations" :key="explanation.code">{{ explanation.message }}</p></div></section><div v-else-if="hasProject" class="empty-state">选择场景后运行一次对比。</div>
    </template>
  </div>
</template>

<style scoped>
.resources-view > .workspace-tabs { margin: 0 0 4px; }.toolbar { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }.toolbar label { display: flex; align-items: center; gap: 8px; color: var(--muted); font-size: 12px; }.toolbar select, .search-box input { min-height: 34px; padding: 0 10px; border: 1px solid var(--line); border-radius: 8px; background: #fff; }.search-box { display: flex; align-items: center; gap: 8px; max-width: 420px; margin-top: 16px; color: var(--muted); }.search-box input { flex: 1; }.resource-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 14px; }.resource-card { padding: 18px; border: 1px solid var(--line); border-radius: 12px; background: #fff; cursor: pointer; transition: .18s ease; }.resource-card:hover, .resource-card.selected { border-color: #8eb4f5; box-shadow: 0 8px 24px rgba(47,111,237,.1); }.resource-card h3 { margin: 8px 0; color: var(--ink); }.resource-card p { min-height: 45px; color: var(--muted); line-height: 1.6; }.card-kicker, .chip { color: var(--muted); font-size: 11px; }.chip-row { display: flex; gap: 6px; flex-wrap: wrap; }.chip { padding: 4px 8px; background: #eef4ff; border-radius: 99px; color: #3568bd; }.detail-panel { margin-top: 14px; }.resource-columns { display: grid; grid-template-columns: .8fr 1.2fr; gap: 24px; }.plain-list { display: grid; gap: 10px; padding: 0; list-style: none; }.plain-list li { display: grid; gap: 3px; padding: 10px 12px; background: #f6f8fc; border-radius: 8px; }.plain-list span { color: var(--muted); font-size: 12px; }.comparison-header { display: flex; justify-content: space-between; align-items: center; }.status-good, .status-bad { display: inline-flex; align-items: center; gap: 4px; font-size: 12px; }.status-good { color: #16845a; }.status-bad { color: #c04444; }.rule-note { margin: 12px 0; color: var(--muted); font-size: 12px; }.explanation-list { margin-top: 16px; color: var(--muted); font-size: 12px; }.explanation-list p { margin: 7px 0; }small { display: block; color: var(--muted); }@media (max-width: 850px) { .resource-grid, .resource-columns { grid-template-columns: 1fr; } }
</style>
