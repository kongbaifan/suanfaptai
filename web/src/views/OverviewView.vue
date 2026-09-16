<script setup lang="ts">
import { computed } from "vue";
import { ArrowRight, CheckCircle2, ClipboardCheck, Layers3, Play, Save, TriangleAlert } from "lucide-vue-next";
import { useDiagnosticsStore } from "@/stores/diagnostics";
import { useEvaluationStore } from "@/stores/evaluation";
import { useProjectStore } from "@/stores/project";
import { useRealtimeStore } from "@/stores/realtime";
import { useUiStore, type ViewName } from "@/stores/ui";
import { finite, valueWithUnit } from "@/utils/format";

const project = useProjectStore();
const evaluation = useEvaluationStore();
const realtime = useRealtimeStore();
const diagnostics = useDiagnosticsStore();
const ui = useUiStore();

const projectData = computed(() => project.project);
const issueCount = computed(() => evaluation.result?.issues?.length ?? 0);
const errorCount = computed(() => (evaluation.result?.issues ?? []).filter((issue) => issue.level === "error").length);
const warningCount = computed(() => (evaluation.result?.issues ?? []).filter((issue) => issue.level === "warning").length);
const rooms = computed(() => realtime.rooms?.rooms ?? []);
const comfortRooms = computed(() => rooms.value.filter((room) => Number.isFinite(room.relative_humidity_pct) && Number.isFinite(room.target_humidity_pct) && room.relative_humidity_pct <= room.target_humidity_pct));
const projectName = computed(() => project.active?.name?.replace("（未保存）", "") || "未命名教学项目");
const projectMode = computed(() => project.usingSample ? "教学样例" : "已保存项目");
const floors = computed(() => [...(projectData.value?.floors ?? [])].sort((a, b) => b.level - a.level).map((floor) => ({
  ...floor,
  rooms: (projectData.value?.rooms ?? []).filter((room) => room.floor_id === floor.id),
  devices: (projectData.value?.devices ?? []).filter((device) => device.floor_id === floor.id),
})));
const modeLabels: Record<string, string> = { cooling: '制冷', heating: '制热', ventilation: '通风', standby: '待机', fault: '故障实验' };

function go(view: ViewName) {
  ui.navigate(view);
}

async function evaluate() {
  if (project.project && project.projectId) await evaluation.evaluate(project.projectId, project.project);
}

async function saveSample() {
  await project.saveSample();
}
</script>

<template>
  <div class="view-stack overview-view">
    <section class="overview-hero">
      <div class="overview-hero-copy">
        <p class="eyebrow">PROJECT WORKSPACE / {{ projectMode }}</p>
        <h1>{{ projectName }}</h1>
        <p>从系统搭建开始，完成设计校核，再进入实时实验。每一步都能看到计算依据和整改方向。</p>
        <div class="overview-hero-actions">
          <button class="button primary" type="button" @click="go('building')"><Layers3 />继续搭建</button>
          <button class="button secondary" type="button" :disabled="evaluation.state === 'loading' || !project.project" @click="evaluate"><ClipboardCheck />{{ evaluation.state === 'loading' ? '评估中…' : '运行系统评估' }}</button>
          <button v-if="project.usingSample" class="button quiet" type="button" :disabled="project.saving" @click="saveSample"><Save />保存样例</button>
        </div>
      </div>
      <div class="building-section" aria-label="项目楼层示意">
        <div class="building-section-heading"><span>建筑剖面</span><small>按楼层组织系统</small></div>
        <div v-if="floors.length" class="building-section-levels"><button v-for="floor in floors" :key="floor.id" class="building-level" @click="go('building')"><span class="level-label">{{ floor.name || floor.id }}</span><span class="level-rooms"><i v-for="room in floor.rooms.slice(0, 5)" :key="room.id ?? room.name" :title="room.name"></i><small v-if="!floor.rooms.length">{{ floor.devices.length ? '设备区域' : '无房间' }}</small></span><small>{{ floor.rooms.length }} 间</small></button></div>
        <p v-else class="building-section-empty">添加楼层后，在这里查看建筑结构。</p>
        <div class="building-section-caption"><i></i>房间分区<span>{{ projectData?.connections.length ?? 0 }} 条系统连接</span></div>
      </div>
    </section>

    <section class="overview-stat-grid">
      <article class="overview-stat"><span>楼层</span><strong>{{ projectData?.floors.length ?? 0 }}</strong><small>结构节点</small></article>
      <article class="overview-stat"><span>房间</span><strong>{{ projectData?.rooms.length ?? 0 }}</strong><small>热湿负荷单元</small></article>
      <article class="overview-stat"><span>设备</span><strong>{{ projectData?.devices.length ?? 0 }}</strong><small>系统设备</small></article>
      <article class="overview-stat"><span>实时连接</span><strong>{{ realtime.socketState === 'connected' ? '在线' : '待连接' }}</strong><small>{{ realtime.running ? '仿真运行中' : '尚未运行' }}</small></article>
    </section>

    <section class="overview-workflow">
      <div class="section-title"><div><p class="eyebrow">A CLEAR PATH</p><h2>现在要做什么？</h2></div><span>按顺序完成一轮实验</span></div>
      <div class="workflow-grid">
        <button class="workflow-card" type="button" @click="go('building')"><span class="workflow-index">01</span><div><strong>搭建系统</strong><p>编辑楼层、房间、设备和管网连接</p></div><ArrowRight /></button>
        <button class="workflow-card" type="button" @click="go('evaluation')"><span class="workflow-index">02</span><div><strong>设计校核</strong><p>查看负荷、拓扑、水力与风系统</p></div><ArrowRight /></button>
        <button class="workflow-card" type="button" @click="go('runtime')"><span class="workflow-index">03</span><div><strong>实时实验</strong><p>启动仿真，观察房间和设备响应</p></div><ArrowRight /></button>
        <button class="workflow-card" type="button" @click="go('history')"><span class="workflow-index">04</span><div><strong>复盘结果</strong><p>回看评估记录和实时趋势</p></div><ArrowRight /></button>
      </div>
    </section>

    <div class="overview-columns">
      <section class="band-section overview-panel">
        <div class="section-title"><div><p class="eyebrow">LATEST CHECK</p><h2>最近一次校核</h2></div><button class="button ghost" type="button" @click="go('evaluation')">查看详情 <ArrowRight /></button></div>
        <div v-if="evaluation.result" class="result-summary">
          <div class="result-lead"><span>修正冷负荷</span><strong>{{ valueWithUnit(evaluation.result.load?.adjusted_cooling_kw, 'kW') }}</strong><small>{{ issueCount }} 项提示 · {{ errorCount }} 项错误 · {{ warningCount }} 项告警</small></div>
          <div class="result-list"><div><span>水泵扬程</span><strong>{{ valueWithUnit(evaluation.result.water?.pump_head_m, 'm') }}</strong></div><div><span>拓扑节点</span><strong>{{ evaluation.result.topology?.node_count ?? 0 }}</strong></div><div><span>估算房温</span><strong>{{ valueWithUnit(evaluation.result.simulation?.estimated_room_temp_c, '°C') }}</strong></div></div>
        </div>
        <div v-else class="empty-state overview-empty"><ClipboardCheck /><div><strong>本次尚未评估</strong><span v-if="evaluation.history.length">已有 {{ evaluation.history.length }} 次历史评估，可到实验复盘查看，或重新评估当前结构。</span><span v-else>运行一次系统评估，查看负荷、管网与需要处理的问题。</span></div><button class="button primary" type="button" :disabled="!project.project || evaluation.state === 'loading'" @click="evaluate">{{ evaluation.state === 'loading' ? '评估中…' : '开始评估' }}</button></div>
      </section>

      <section class="band-section overview-panel">
        <div class="section-title"><div><p class="eyebrow">LIVE SNAPSHOT</p><h2>实时实验</h2></div><button class="button ghost" type="button" @click="go('runtime')">打开实验 <ArrowRight /></button></div>
        <div class="live-summary">
          <div class="live-state" :class="{ running: realtime.running }"><span class="live-dot"></span><strong>{{ realtime.running ? '仿真运行中' : '仿真已停止' }}</strong><small>{{ realtime.current?.updated_at ? new Date(realtime.current.updated_at).toLocaleTimeString('zh-CN', { hour12: false }) : '等待第一帧数据' }}</small></div>
          <div class="live-numbers"><div><span>{{ realtime.running ? '当前房间读数' : '最近房间读数' }}</span><strong>{{ rooms.length }} 间</strong><small>{{ comfortRooms.length }} 间湿度不高于目标</small></div><div><span>当前模式</span><strong>{{ modeLabels[realtime.config.mode] ?? '未设置' }}</strong><small>目标 {{ finite(realtime.config.target_temp_c, 1) }}℃ / {{ finite(realtime.config.target_humidity_pct, 0) }}%</small></div></div>
        </div>
      </section>
    </div>

    <div v-if="evaluation.error" class="error-banner" role="alert">{{ evaluation.error }}</div>
    <section class="overview-bottom-grid">
      <div class="overview-health"><CheckCircle2 v-if="diagnostics.healthy" /><TriangleAlert v-else /><div><strong>{{ diagnostics.healthy ? '服务状态正常' : '服务需要关注' }}</strong><span>{{ diagnostics.report ? `${diagnostics.report.data.catalog_items} 条设备目录记录 · ${diagnostics.report.data.knowledge_entries} 条知识条目` : '正在读取服务诊断…' }}</span></div><button class="button ghost" type="button" @click="go('diagnostics')">查看诊断 <ArrowRight /></button></div>
      <div class="overview-tip"><Play /><div><strong>教学建议</strong><span>先运行设计校核，再启动实时仿真。保存样例后才能体验历史记录、设备控制和教学对比。</span></div></div>
    </section>
  </div>
</template>

<style scoped>
.overview-view { --overview-pad: clamp(24px, 3vw, 44px); background: var(--surface-soft); padding-bottom: 20px; }
.overview-hero { display: grid; grid-template-columns: minmax(0, 1fr) 340px; gap: 40px; padding: 36px var(--overview-pad); background: #fff; border-bottom: 1px solid var(--line); align-items: center; }
.overview-hero h1 { max-width: 720px; color: var(--teal-dark); font-size: clamp(28px, 3vw, 42px); letter-spacing: -.03em; overflow-wrap: anywhere; }
.overview-hero-copy > p:not(.eyebrow) { max-width: 590px; margin-top: 15px; color: var(--muted); font-size: 14px; line-height: 1.85; }
.overview-hero-actions { display: flex; flex-wrap: wrap; gap: 10px; margin-top: 25px; }
.button.quiet { color: var(--teal); background: #eef5ff; }
.building-section { padding: 20px; color: #d9e9fa; background: #193a63; border: 1px solid #2e5380; border-radius: 14px; background-image: linear-gradient(#ffffff05 1px, transparent 1px), linear-gradient(90deg, #ffffff05 1px, transparent 1px); background-size: 16px 16px; }
.building-section-heading { display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; }.building-section-heading span { font-size: 14px; font-weight: 600; }.building-section-heading small { color: #acc5de; font-size: 11px; }
.building-section-levels { display: grid; gap: 7px; max-height: 216px; overflow: auto; padding-right: 4px; }.building-level { min-height: 36px; display: flex; gap: 9px; align-items: center; width: 100%; color: #d9e9fa; background: #244773; border: 1px solid #5076a1; border-right: 3px solid #93c9ed; border-radius: 3px; padding: 7px 10px; text-align: left; }.building-level:hover { background: #315886; }.level-label { max-width: 96px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 12px; }.level-rooms { flex: 1; display: flex; align-items: center; gap: 5px; }.level-rooms i { width: 21px; height: 17px; border: 1px solid #8fbce2; border-radius: 2px; background: #5489b2; }.building-level small { color: #bdd4e8; font-size: 10px; }.building-section-caption { display: flex; align-items: center; gap: 6px; margin-top: 16px; color: #bed3e7; font-size: 10px; }.building-section-caption i { width: 9px; height: 9px; border: 1px solid #a2c7e5; background: #5489b2; }.building-section-caption span { margin-left: auto; }.building-section-empty { font-size: 12px; line-height: 1.7; color: #bed3e7; }
.overview-stat-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 14px; padding: 22px var(--overview-pad) 0; }.overview-stat { display: grid; gap: 7px; padding: 20px 22px; background: #fff; border: 1px solid var(--line); border-radius: 12px; }.overview-stat span, .overview-stat small { color: var(--muted); font-size: 12px; }.overview-stat strong { color: var(--teal-dark); font-size: 26px; font-variant-numeric: tabular-nums; }
.overview-workflow { padding: 28px var(--overview-pad) 20px; }.workflow-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; }.workflow-card { display: grid; grid-template-columns: auto 1fr auto; align-items: start; gap: 10px; min-height: 120px; padding: 18px 15px; color: var(--teal-dark); text-align: left; background: #fff; border: 1px solid var(--line); border-radius: 12px; transition: border-color .16s, box-shadow .16s; }.workflow-card:hover { border-color: #8db5e1; box-shadow: 0 4px 16px #1c36550d; }.workflow-card > svg { width: 15px; color: #6f9ed0; }.workflow-card div { min-width: 0; display: grid; gap: 8px; }.workflow-card strong { font-size: 14px; }.workflow-card p { color: var(--muted); font-size: 12px; line-height: 1.7; }.workflow-index { color: #7098bd; font-size: 11px; font-weight: 600; padding-top: 2px; }
.overview-columns, .overview-bottom-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; padding: 0 var(--overview-pad) 16px; }.overview-panel { min-width: 0; padding: 22px; border: 1px solid var(--line); border-radius: 12px; }.overview-panel .section-title { margin-bottom: 18px; }.overview-panel h2 { font-size: 17px; }.overview-panel .section-title .button { font-size: 12px; white-space: nowrap; }
.result-summary { display: grid; gap: 14px; }.result-lead { display: grid; gap: 6px; padding: 16px; border-radius: 10px; background: #f1f7fd; }.result-lead span, .result-lead small, .result-list span { color: var(--muted); font-size: 12px; }.result-lead strong { color: var(--teal-dark); font-size: 28px; }.result-list { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; }.result-list > div { min-width: 0; display: grid; gap: 5px; padding: 10px 0; }.result-list strong { color: var(--teal-dark); font-size: 14px; }.overview-empty { display: grid; grid-template-columns: auto 1fr; align-items: start; gap: 12px; padding: 18px; }.overview-empty > svg { color: #6a9ad0; }.overview-empty div { display: grid; gap: 6px; }.overview-empty span { color: var(--muted); font-size: 12px; line-height: 1.7; }.overview-empty .button { grid-column: 2; justify-self: start; margin-top: 3px; }
.live-summary { display: grid; gap: 18px; }.live-state { display: flex; flex-wrap: wrap; align-items: center; gap: 9px; padding: 14px; color: #586c80; background: #f4f7fa; border-radius: 10px; }.live-state.running { color: #226b59; background: #edf8f3; }.live-state small { margin-left: auto; color: #7d8f9f; font-size: 11px; }.live-dot { width: 8px; height: 8px; border-radius: 50%; background: #aab8c6; }.live-state.running .live-dot { background: #35aa78; }.live-numbers { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }.live-numbers > div { display: grid; gap: 7px; padding: 8px 0; }.live-numbers span, .live-numbers small { color: var(--muted); font-size: 11px; }.live-numbers strong { color: var(--teal-dark); font-size: 18px; }
.overview-health, .overview-tip { display: flex; align-items: center; gap: 12px; min-height: 76px; padding: 16px 18px; border-radius: 12px; background: #fff; border: 1px solid var(--line); }.overview-health > svg { color: #2e9a72; flex-shrink: 0; }.overview-health > div, .overview-tip > div { flex: 1; display: grid; gap: 5px; }.overview-health strong, .overview-tip strong { font-size: 13px; }.overview-health span, .overview-tip span { color: var(--muted); font-size: 12px; line-height: 1.65; }.overview-tip { background: #edf5fe; border-color: #d5e6f9; }.overview-tip > svg { color: #4d8fc9; flex-shrink: 0; }
@media (max-width: 1180px) { .overview-hero { grid-template-columns: 1fr 270px; gap: 24px; }.workflow-grid { grid-template-columns: repeat(2, 1fr); }.overview-bottom-grid { grid-template-columns: 1fr; } }
@media (max-width: 960px) { .overview-hero { grid-template-columns: 1fr; }.building-section { max-width: 100%; }.overview-columns { grid-template-columns: 1fr; } }
@media (max-width: 600px) { .overview-view { --overview-pad: 18px; }.overview-hero { padding-top: 24px; }.overview-stat-grid { grid-template-columns: 1fr 1fr; gap: 10px; }.overview-stat { padding: 15px; }.overview-health { flex-wrap: wrap; }.overview-panel { padding: 18px; }.overview-panel .section-title { flex-wrap: wrap; }.workflow-card { grid-template-columns: 1fr; gap: 6px; }.workflow-card > svg { display: none; } }
</style>
