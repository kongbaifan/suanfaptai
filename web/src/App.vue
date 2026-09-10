<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { Activity, BarChart3, BookOpen, Building2, ChevronDown, History, Menu, Network, Plus, Save, Settings2, X } from "lucide-vue-next";
import StatusBadge from "@/components/StatusBadge.vue";
import { useDiagnosticsStore } from "@/stores/diagnostics";
import { useEvaluationStore } from "@/stores/evaluation";
import { SAMPLE_OPTIONS, sampleNameForSelection, useProjectStore } from "@/stores/project";
import { useRealtimeStore } from "@/stores/realtime";
import { useUiStore, type ViewName } from "@/stores/ui";
import BuildingView from "@/views/BuildingView.vue";
import CatalogView from "@/views/CatalogView.vue";
import DiagnosticsView from "@/views/DiagnosticsView.vue";
import EvaluationView from "@/views/EvaluationView.vue";
import HistoryView from "@/views/HistoryView.vue";
import RuntimeView from "@/views/RuntimeView.vue";

const project = useProjectStore();
const evaluation = useEvaluationStore();
const realtime = useRealtimeStore();
const diagnostics = useDiagnosticsStore();
const ui = useUiStore();
const sampleProjectName = ref("");
const blankProjectName = ref("");

const views: Record<ViewName, typeof BuildingView> = { building: BuildingView, evaluation: EvaluationView, runtime: RuntimeView, history: HistoryView, catalog: CatalogView, diagnostics: DiagnosticsView };
const activeView = computed(() => views[ui.view]);
const nav = [
  { id: "building" as const, label: "建筑结构", icon: Network },
  { id: "evaluation" as const, label: "系统评估", icon: BarChart3 },
  { id: "runtime" as const, label: "实时运行", icon: Activity },
  { id: "history" as const, label: "历史趋势", icon: History },
  { id: "catalog" as const, label: "设备参数库", icon: BookOpen },
  { id: "diagnostics" as const, label: "服务诊断", icon: Settings2 },
];

async function selectProject(event: Event) {
  const id = (event.target as HTMLSelectElement).value;
  const sampleName = sampleNameForSelection(id);
  if (sampleName) await project.loadSample(sampleName); else await project.select(id);
}

async function saveSampleAsProject() {
  const saved = await project.saveSample(sampleProjectName.value);
  if (saved) sampleProjectName.value = "";
}

async function createBlankProject() {
  const saved = await project.createBlankProject(blankProjectName.value);
  if (saved) {
    blankProjectName.value = "";
    ui.navigate("building");
  }
}

watch(
  () => [project.usingSample, project.activeSampleName] as const,
  () => {
    sampleProjectName.value = project.usingSample ? project.sampleDefaultName : "";
  },
  { immediate: true },
);

watch(() => project.selectionKey, async (selectionKey, previous) => {
  if (!selectionKey || selectionKey === previous) return;
  evaluation.reset();
  const projectId = project.projectId;
  if (!projectId || project.usingSample) realtime.disconnect();
  else {
    await realtime.activate(projectId);
    await evaluation.loadHistory(projectId);
  }
});

onMounted(async () => {
  await Promise.all([project.initialize(), diagnostics.refresh()]);
});
onBeforeUnmount(() => realtime.disconnect());
</script>

<template>
  <div class="app-shell">
    <header class="topbar">
      <button class="mobile-menu icon-button" title="打开导航" @click="ui.sidebarOpen = true"><Menu /></button>
      <div class="brand"><div class="brand-mark"><Building2 /></div><div><strong>中央空调运行工作台</strong><span>HVAC Runtime Console</span></div></div>
      <div class="project-picker">
        <label>当前项目</label>
        <div class="select-shell"><select :value="project.selectionKey ?? ''" @change="selectProject"><option v-for="sample in SAMPLE_OPTIONS" :key="sample.selectionKey" :value="sample.selectionKey">{{ sample.label }}</option><option v-for="item in project.projects" :key="item.project_id" :value="item.project_id">{{ item.name }}</option></select><ChevronDown /></div>
        <form v-if="project.usingSample" class="sample-save-form" @submit.prevent="saveSampleAsProject">
          <input v-model="sampleProjectName" class="sample-name-input" aria-label="新项目名称" :placeholder="project.sampleDefaultName" />
          <button class="button secondary save-sample" type="submit" :disabled="project.saving"><Save />{{ project.saving ? "保存中" : "保存为项目" }}</button>
        </form>
        <form class="blank-create-form" @submit.prevent="createBlankProject">
          <input v-model="blankProjectName" class="sample-name-input" aria-label="空白项目名称" placeholder="新建教学项目" />
          <button class="button secondary save-sample" type="submit" :disabled="project.saving" title="从零新建一个不含楼层房间的空白项目"><Plus />{{ project.saving ? "创建中" : "新建空白项目" }}</button>
        </form>
      </div>
      <div class="global-status"><StatusBadge :tone="realtime.socketState === 'connected' ? 'ok' : 'warn'" label="API / WS" /><StatusBadge :tone="diagnostics.report ? (diagnostics.healthy ? 'ok' : 'error') : 'warn'" label="服务诊断" /></div>
    </header>

    <div class="shell-body">
      <div v-if="ui.sidebarOpen" class="sidebar-backdrop" @click="ui.sidebarOpen = false"></div>
      <aside class="sidebar" :class="{ open: ui.sidebarOpen }">
        <button class="close-sidebar icon-button" title="关闭导航" @click="ui.sidebarOpen = false"><X /></button>
        <nav><button v-for="item in nav" :key="item.id" :class="{ active: ui.view === item.id }" @click="ui.navigate(item.id)"><component :is="item.icon" /><span>{{ item.label }}</span></button></nav>
        <div class="sidebar-footer"><span>数据更新</span><strong>{{ realtime.current?.updated_at ? new Date(realtime.current.updated_at).toLocaleTimeString('zh-CN', { hour12: false }) : '--' }}</strong><small v-if="realtime.stale">实时数据可能已过期</small></div>
      </aside>
      <main class="main-content"><div v-if="project.error" class="error-banner global-error">项目加载失败：{{ project.error }}</div><div v-if="project.saveError" class="error-banner global-error">项目保存失败：{{ project.saveError }}</div><component :is="activeView" /></main>
    </div>
  </div>
</template>

<style scoped>
.sample-save-form, .blank-create-form { display: flex; align-items: center; gap: 8px; min-width: 0; }
.blank-create-form .sample-name-input { width: min(180px, 15vw); }
.sample-name-input { width: min(230px, 22vw); height: 36px; padding: 0 10px; color: #fff; background: #285157; border: 1px solid #769196; border-radius: 4px; }
.sample-name-input::placeholder { color: #b9ccce; }
.sample-name-input:focus { border-color: #8ee2e4; box-shadow: 0 0 0 2px rgba(142,226,228,.15); }
@media (max-width: 1100px) {
  .sample-name-input { width: min(190px, 20vw); }
  .blank-create-form .sample-name-input { width: min(140px, 14vw); }
}
@media (max-width: 820px) {
  .sample-save-form { position: absolute; top: 64px; left: 12px; right: 12px; z-index: 32; display: flex; min-width: 0; padding: 8px; background: var(--teal-dark); border: 1px solid #52757a; border-radius: 0 0 4px 4px; box-shadow: 0 8px 18px rgba(0,0,0,.18); }
  .sample-save-form .save-sample { display: inline-flex !important; }
  .sample-name-input { flex: 1 1 auto; width: auto; min-width: 0; }
  .blank-create-form .sample-name-input { display: none; }
}
</style>
