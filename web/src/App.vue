<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch, type Component } from "vue";
import { BookOpen, Building2, Calculator, ChevronDown, ClipboardCheck, Gauge, History, LifeBuoy, Menu, Network, PlayCircle, Save, X } from "lucide-vue-next";
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
import LoadView from "@/views/LoadView.vue";
import OverviewView from "@/views/OverviewView.vue";
import ResourcesView from "@/views/ResourcesView.vue";
import RuntimeView from "@/views/RuntimeView.vue";

const project = useProjectStore();
const evaluation = useEvaluationStore();
const realtime = useRealtimeStore();
const diagnostics = useDiagnosticsStore();
const ui = useUiStore();
const managerOpen = ref(false);

const views: Record<ViewName, Component> = { overview: OverviewView, building: BuildingView, load: LoadView, evaluation: EvaluationView, runtime: RuntimeView, history: HistoryView, catalog: CatalogView, diagnostics: DiagnosticsView, resources: ResourcesView };
const activeView = computed(() => views[ui.view]);
const navGroups = [
  { label: "项目工作区", items: [
    { id: "overview" as const, label: "项目总览", hint: "开始与进度", icon: Gauge },
    { id: "building" as const, label: "结构建模", hint: "楼层与管网", icon: Network },
  ] },
  { label: "实验流程", items: [
    { id: "load" as const, label: "负荷计算", hint: "分项与指标", icon: Calculator },
    { id: "evaluation" as const, label: "设计校核", hint: "负荷与系统", icon: ClipboardCheck },
    { id: "runtime" as const, label: "实时实验", hint: "运行与控制", icon: PlayCircle },
    { id: "history" as const, label: "实验复盘", hint: "趋势与记录", icon: History },
  ] },
  { label: "资料与工具", items: [
    { id: "resources" as const, label: "设备与教学", hint: "知识 · 选型 · 对比", icon: BookOpen },
    { id: "diagnostics" as const, label: "服务状态", hint: "健康与资源", icon: LifeBuoy },
  ] },
] as const;
const viewMeta: Record<ViewName, { label: string; trail: string }> = {
  overview: { label: "项目总览", trail: "项目工作区" }, building: { label: "结构建模", trail: "项目工作区" },
  load: { label: "负荷计算", trail: "实验流程" }, evaluation: { label: "设计校核", trail: "实验流程" },
  runtime: { label: "实时实验", trail: "实验流程" },
  history: { label: "实验复盘", trail: "实验流程" }, resources: { label: "设备与教学", trail: "资料与工具" },
  catalog: { label: "参数目录", trail: "资料与工具" }, diagnostics: { label: "服务状态", trail: "资料与工具" },
};

/** 把当前未保存的样例落库成正式项目。 */
async function saveCurrentSample() {
  await project.saveSample();
}

/** 面板里新建/复制后切到建筑结构页，符合"开始编辑"的直觉。 */
function onProjectCreated() {
  ui.navigate("building");
  managerOpen.value = false;
}

watch([() => project.selectionKey, () => project.project], async ([selectionKey, data], [previousKey, previousData]) => {
  if (!selectionKey || (selectionKey === previousKey && data === previousData)) return;
  evaluation.reset();
  const projectId = project.projectId;
  if (!projectId || project.usingSample) realtime.disconnect();
  else {
    await realtime.activate(projectId);
    if (project.selectionKey === selectionKey) await evaluation.loadHistory(projectId);
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
      <button class="brand" type="button" @click="ui.navigate('overview')"><div class="brand-mark"><Building2 /></div><div><strong>中央空调教学工作台</strong><span>HVAC LEARNING STUDIO</span></div></button>
      <div class="project-picker">
        <span class="project-context">当前项目</span>
        <button class="project-trigger" type="button" @click="managerOpen = true">
          <span class="project-trigger-dot" :class="{ sample: project.usingSample }"></span><span class="project-trigger-name">{{ project.active?.name ?? "未选择项目" }}</span>
          <ChevronDown />
        </button>
        <button v-if="project.usingSample" class="button secondary save-sample" type="button" :disabled="project.saving" @click="saveCurrentSample"><Save />{{ project.saving ? "保存中" : "保存为项目" }}</button>
      </div>
      <div class="global-status"><StatusBadge :tone="diagnostics.report ? (diagnostics.healthy ? 'ok' : 'error') : 'warn'" :label="diagnostics.report ? (diagnostics.healthy ? '服务正常' : '服务异常') : '服务检查中'" /><StatusBadge :tone="realtime.socketState === 'connected' ? 'ok' : realtime.socketState === 'connecting' || realtime.socketState === 'reconnecting' ? 'warn' : 'muted'" :label="realtime.socketState === 'connected' ? '实时已连接' : realtime.socketState === 'reconnecting' ? '正在重连' : '实时待命'" /></div>
    </header>

    <div class="shell-body">
      <div v-if="ui.sidebarOpen" class="sidebar-backdrop" @click="ui.sidebarOpen = false"></div>
      <aside class="sidebar" :class="{ open: ui.sidebarOpen }">
        <button class="close-sidebar icon-button" title="关闭导航" @click="ui.sidebarOpen = false"><X /></button>
        <nav><div v-for="group in navGroups" :key="group.label" class="nav-group"><p>{{ group.label }}</p><button v-for="item in group.items" :key="item.id" :class="{ active: ui.view === item.id }" @click="ui.navigate(item.id)"><component :is="item.icon" /><span><strong>{{ item.label }}</strong><small>{{ item.hint }}</small></span></button></div></nav>
        <div class="sidebar-footer"><span>最近运行读数</span><strong>{{ realtime.current?.updated_at ? new Date(realtime.current.updated_at).toLocaleTimeString('zh-CN', { hour12: false }) : '等待实时数据' }}</strong><small v-if="realtime.running && realtime.stale">实时数据可能已过期</small><small v-else>中央空调教学实验</small></div>
      </aside>
      <ProjectManager v-if="managerOpen" @close="managerOpen = false" @created="onProjectCreated" />
      <main class="main-content"><div class="page-context"><span>{{ viewMeta[ui.view].trail }}</span><ChevronDown /><strong>{{ viewMeta[ui.view].label }}</strong><em v-if="project.usingSample">未保存样例</em><em class="saved" v-else-if="project.active">已保存项目</em></div><div v-if="ui.buildingDirty && ui.view !== 'building'" class="draft-banner"><span>结构修改尚未保存，当前计算使用上一次保存的数据。</span><button class="button ghost" @click="ui.navigate('building')">返回保存</button></div><div v-if="project.error" class="error-banner global-error">项目加载失败：{{ project.error }}</div><div v-if="project.saveError" class="error-banner global-error">项目保存失败：{{ project.saveError }}</div><KeepAlive><component :is="activeView" /></KeepAlive></main>
    </div>
  </div>
</template>

<style scoped>
.project-picker .save-sample { white-space: nowrap; }
.brand { border: 0; padding: 0; color: inherit; background: transparent; text-align: left; cursor: pointer; }
.project-trigger { display: inline-flex; align-items: center; gap: 8px; max-width: min(360px, 32vw); height: 40px; padding: 0 12px; font-size: 13px; color: #fff; background: #284b72; border: 1px solid #6685a6; border-radius: 9px; cursor: pointer; }
.project-trigger:hover { border-color: #a5c9ec; }
.project-trigger-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.project-trigger svg { width: 15px; height: 15px; flex: none; }
.project-trigger-dot { width: 7px; height: 7px; flex: 0 0 7px; border-radius: 50%; background: #7cd6ae; }
.project-trigger-dot.sample { background: #f6c86e; }
@media (max-width: 1100px) {
  .project-trigger { max-width: min(200px, 24vw); }
}
</style>
