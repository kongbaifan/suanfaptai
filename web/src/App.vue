<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { Activity, BarChart3, BookOpen, Building2, ChevronDown, History, Menu, Network, Save, Settings2, X } from "lucide-vue-next";
import StatusBadge from "@/components/StatusBadge.vue";
import ProjectManager from "@/components/ProjectManager.vue";
import { useDiagnosticsStore } from "@/stores/diagnostics";
import { useEvaluationStore } from "@/stores/evaluation";
import { useProjectStore } from "@/stores/project";
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
const managerOpen = ref(false);

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

/** 把当前未保存的样例落库成正式项目。 */
async function saveCurrentSample() {
  await project.saveSample();
}

/** 面板里新建/复制后切到建筑结构页，符合"开始编辑"的直觉。 */
function onProjectCreated() {
  ui.navigate("building");
  managerOpen.value = false;
}

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
        <button class="project-trigger" type="button" @click="managerOpen = true">
          <span class="project-trigger-name">{{ project.usingSample ? "未保存样例" : (project.active?.name ?? "未选择项目") }}</span>
          <ChevronDown />
        </button>
        <button v-if="project.usingSample" class="button secondary save-sample" type="button" :disabled="project.saving" @click="saveCurrentSample"><Save />{{ project.saving ? "保存中" : "保存为项目" }}</button>
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
      <ProjectManager v-if="managerOpen" @close="managerOpen = false" @created="onProjectCreated" />
      <main class="main-content"><div v-if="project.error" class="error-banner global-error">项目加载失败：{{ project.error }}</div><div v-if="project.saveError" class="error-banner global-error">项目保存失败：{{ project.saveError }}</div><component :is="activeView" /></main>
    </div>
  </div>
</template>

<style scoped>
.project-picker .save-sample { white-space: nowrap; }
.project-trigger { display: inline-flex; align-items: center; gap: 8px; max-width: min(320px, 30vw); height: 36px; padding: 0 10px; font-size: 13px; color: #fff; background: #285157; border: 1px solid #769196; border-radius: 4px; cursor: pointer; }
.project-trigger:hover { border-color: #8ee2e4; }
.project-trigger-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.project-trigger svg { width: 15px; height: 15px; flex: none; }
@media (max-width: 1100px) {
  .project-trigger { max-width: min(200px, 24vw); }
}
</style>
