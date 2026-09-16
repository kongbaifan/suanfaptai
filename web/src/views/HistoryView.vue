<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { Activity, BarChart3, Clock3, History, RefreshCw, TriangleAlert } from "lucide-vue-next";
import StatusBadge from "@/components/StatusBadge.vue";
import { useEvaluationStore } from "@/stores/evaluation";
import { useProjectStore } from "@/stores/project";
import { useRealtimeStore } from "@/stores/realtime";
import { dateTime, finite } from "@/utils/format";

type HistoryPoint = Record<string, unknown>;
const project = useProjectStore();
const evaluation = useEvaluationStore();
const realtime = useRealtimeStore();
const activeMetric = ref<"temperature" | "humidity" | "power">("temperature");
const visiblePoints = ref(36);
const refreshing = ref(false);
const modeLabels: Record<string, string> = { cooling: "制冷", heating: "制热", ventilation: "通风", standby: "待机", fault: "故障" };

const runtimePoints = computed<HistoryPoint[]>(() => realtime.history.slice(-visiblePoints.value));
const metricDefinitions = {
  temperature: { label: "平均室温", unit: "°C", keys: ["average_room_temp_c", "room_temp_c"], color: "#2e7784" },
  humidity: { label: "平均湿度", unit: "%", keys: ["average_room_humidity_pct", "room_humidity_pct"], color: "#c27b2b" },
  power: { label: "总功率", unit: "kW", keys: ["total_power_kw", "system_power_kw"], color: "#5b6eb5" },
} as const;
const selectedMetric = computed(() => metricDefinitions[activeMetric.value]);
const trendValues = computed(() => runtimePoints.value.map((point, index) => {
  const summary = (point.summary ?? point) as Record<string, unknown>;
  const key = selectedMetric.value.keys.find((candidate) => typeof summary[candidate] === "number" && Number.isFinite(summary[candidate]));
  const value = key ? Number(summary[key]) : null;
  return { point, index, value, time: point.updated_at ?? point.timestamp };
}).filter((item) => item.value !== null));
const trendMin = computed(() => {
  if (!trendValues.value.length) return 0;
  const values = trendValues.value.map((item) => item.value as number);
  const minimum = Math.min(...values);
  return minimum === Math.max(...values) ? minimum - 1 : minimum;
});
const trendMax = computed(() => {
  if (!trendValues.value.length) return 1;
  const values = trendValues.value.map((item) => item.value as number);
  const maximum = Math.max(...values);
  return maximum === Math.min(...values) ? maximum + 1 : maximum;
});
const latestRuntime = computed(() => runtimePoints.value[runtimePoints.value.length - 1] ?? null);
const latestSummary = computed(() => (latestRuntime.value?.summary ?? {}) as Record<string, unknown>);
const evaluationCount = computed(() => evaluation.history.length);
const latestEvaluation = computed(() => evaluation.history[0] ?? null);
const totalRuntimeAlarms = computed(() => runtimePoints.value.reduce((sum, point) => sum + (Array.isArray(point.alarms) ? point.alarms.length : 0), 0));

function metricPercent(value: number | null): number {
  if (value === null) return 0;
  return ((value - trendMin.value) / (trendMax.value - trendMin.value)) * 100;
}

function valueForSummary(summary: Record<string, unknown>, keys: string[]) {
  const key = keys.find((candidate) => typeof summary[candidate] === "number" && Number.isFinite(summary[candidate]));
  return key ? summary[key] : null;
}

async function refresh() {
  if (project.projectId && project.projectId !== "sample") {
    refreshing.value = true;
    try {
      await Promise.all([evaluation.loadHistory(project.projectId), realtime.loadHistory(project.projectId)]);
    } finally {
      refreshing.value = false;
    }
  }
}

function modeLabel(value: unknown) {
  return typeof value === "string" ? modeLabels[value] ?? value : "--";
}

function loadMore() {
  visiblePoints.value = Math.min(visiblePoints.value + 36, realtime.history.length);
}

onMounted(refresh);
</script>

<template>
  <div class="view-stack history-view">
    <section class="view-heading action-heading">
      <div><p class="eyebrow">HISTORY & TRENDS</p><h1>历史趋势</h1><p>从评估记录与实时采样中回看系统变化，查看历史评估结果和最近运行快照。</p></div>
      <button class="button secondary" title="刷新历史" :disabled="refreshing" @click="refresh"><RefreshCw :class="{ spin: refreshing }" />刷新历史</button>
    </section>

    <section class="history-summary-grid">
      <article><History /><span>评估记录</span><strong>{{ evaluationCount }}</strong><small>{{ latestEvaluation ? dateTime(latestEvaluation.created_at) : "暂无评估" }}</small></article>
      <article><Activity /><span>运行采样</span><strong>{{ realtime.history.length }}</strong><small>{{ latestRuntime ? dateTime(latestRuntime.updated_at ?? latestRuntime.timestamp) : "暂无采样" }}</small></article>
      <article><TriangleAlert /><span>采样告警</span><strong :class="{ 'history-alert': totalRuntimeAlarms > 0 }">{{ totalRuntimeAlarms }}</strong><small>当前列表内累计</small></article>
      <article><BarChart3 /><span>最新总功率</span><strong>{{ finite(valueForSummary(latestSummary, ["total_power_kw"]), 2) }} kW</strong><small>{{ realtime.running ? "仿真运行中" : "最近状态" }}</small></article>
    </section>

    <section class="band-section history-trend-section">
      <div class="section-title"><div><p class="eyebrow">RUNTIME TREND</p><h2>运行趋势</h2><p class="history-note">按采样顺序展示实际记录，缺失读数不会补值。纵轴按当前读数范围缩放。</p></div><div class="history-metric-tabs" role="group" aria-label="趋势指标"><button v-for="(definition, key) in metricDefinitions" :key="key" type="button" :aria-pressed="activeMetric === key" :class="{ active: activeMetric === key }" @click="activeMetric = key">{{ definition.label }}</button></div></div>
      <div v-if="trendValues.length" class="trend-chart" role="img" :aria-label="`${selectedMetric.label}趋势图`">
        <div class="trend-y-labels"><span>{{ finite(trendMax, 1) }} {{ selectedMetric.unit }}</span><span>{{ finite((trendMax + trendMin) / 2, 1) }} {{ selectedMetric.unit }}</span><span>{{ finite(trendMin, 1) }} {{ selectedMetric.unit }}</span></div>
        <div class="trend-plot"><div class="trend-grid-line"></div><div class="trend-grid-line"></div><div class="trend-grid-line"></div><div class="trend-bars"><span v-for="item in trendValues" :key="`${item.index}-${item.time}`" class="trend-bar" :style="{ height: `${metricPercent(item.value)}%`, background: selectedMetric.color }" :title="`${dateTime(item.time)} · ${finite(item.value, 2)} ${selectedMetric.unit}`"></span></div></div>
      </div>
      <div v-else class="empty-state large">当前运行历史没有可绘制的 {{ selectedMetric.label }} 数据。启动仿真并等待采样后再查看趋势。</div>
      <div v-if="trendValues.length" class="trend-axis"><span>{{ dateTime(trendValues[0].time) }}</span><span>{{ dateTime(trendValues[trendValues.length - 1].time) }}</span></div>
      <button v-if="visiblePoints < realtime.history.length" class="button ghost history-more" type="button" @click="loadMore">加载更早采样（{{ realtime.history.length - visiblePoints }}）</button>
    </section>

    <section class="split-band">
      <div class="band-section"><div class="section-title"><div><p class="eyebrow">EVALUATIONS</p><h2>评估记录</h2></div><History /></div><div v-if="evaluation.history.length" class="timeline"><article v-for="item in evaluation.history" :key="item.history_id"><div><strong>{{ dateTime(item.created_at) }}</strong><small>{{ item.issue_count }} 个问题 · {{ item.history_id }}</small><div class="history-record-metrics"><span>冷负荷 <b>{{ finite(item.summary.cooling_load_kw, 1) }} kW</b></span><span>热负荷 <b>{{ finite(item.summary.heating_load_kw, 1) }} kW</b></span><span>房间 <b>{{ item.summary.room_count ?? "--" }}</b></span></div></div><StatusBadge :tone="item.issue_count > 0 ? 'warn' : 'ok'" :label="item.issue_count > 0 ? '含问题' : '未发现问题'" /></article></div><div v-else class="empty-state">该项目还没有评估记录。运行一次系统评估后会出现在这里。</div></div>
      <div class="band-section"><div class="section-title"><div><p class="eyebrow">LATEST SAMPLE</p><h2>最新采样</h2></div><Clock3 /></div><div v-if="latestRuntime" class="kv-grid"><div><span>时间</span><strong>{{ dateTime(latestRuntime.updated_at ?? latestRuntime.timestamp) }}</strong></div><div><span>模式</span><strong>{{ modeLabel(latestSummary.mode) }}</strong></div><div><span>设备数</span><strong>{{ latestSummary.device_count ?? Object.keys((latestRuntime.devices as Record<string, unknown>) ?? {}).length }}</strong></div><div><span>告警</span><strong>{{ Array.isArray(latestRuntime.alarms) ? latestRuntime.alarms.length : 0 }}</strong></div></div><div v-else class="empty-state">启动一次仿真后，这里会显示最新运行采样。</div></div>
    </section>

    <section class="band-section"><div class="section-title"><div><p class="eyebrow">SNAPSHOTS</p><h2>最近运行快照</h2></div><span>{{ runtimePoints.length }} 条</span></div><div v-if="runtimePoints.length" class="table-wrap"><table><thead><tr><th>时间 / Tick</th><th>模式</th><th>平均室温</th><th>平均湿度</th><th>总功率</th><th>告警</th></tr></thead><tbody><tr v-for="(point, index) in [...runtimePoints].reverse()" :key="`${point.timestamp}-${index}`"><td>{{ dateTime(point.updated_at ?? point.timestamp) }}</td><td>{{ modeLabel((point.summary as Record<string, unknown> | undefined)?.mode) }}</td><td>{{ finite((point.summary as Record<string, unknown> | undefined)?.average_room_temp_c, 1) }} °C</td><td>{{ finite((point.summary as Record<string, unknown> | undefined)?.average_room_humidity_pct, 1) }} %</td><td>{{ finite((point.summary as Record<string, unknown> | undefined)?.total_power_kw, 2) }} kW</td><td>{{ Array.isArray(point.alarms) ? point.alarms.length : 0 }}</td></tr></tbody></table></div><div v-else class="empty-state">尚无实时快照。启动仿真后会在这里显示运行历史。</div></section>
  </div>
</template>

<style scoped>
.history-summary-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); background: var(--surface); border-bottom: 1px solid var(--line); }
.history-summary-grid article { min-height: 116px; display: grid; align-content: center; gap: 5px; padding: 20px 22px; border-right: 1px solid var(--line); }
.history-summary-grid article:last-child { border-right: 0; }
.history-summary-grid svg { width: 20px; height: 20px; color: var(--teal); }
.history-summary-grid span, .history-summary-grid small { color: var(--muted); font-size: 11px; }
.history-summary-grid strong { font-size: 23px; font-variant-numeric: tabular-nums; }
.history-summary-grid .history-alert { color: var(--red); }
.history-note { margin-top: 5px; color: var(--muted); font-size: 12px; line-height: 1.5; }
.history-metric-tabs { display: flex; gap: 6px; }
.history-metric-tabs button { min-height: 32px; padding: 0 11px; color: var(--muted); background: var(--surface-soft); border: 1px solid var(--line); border-radius: 999px; font-size: 11px; }
.history-metric-tabs button.active { color: #fff; background: var(--teal); border-color: var(--teal); }
.trend-chart { display: grid; grid-template-columns: 86px minmax(0, 1fr); gap: 12px; height: 220px; }
.trend-y-labels { display: flex; flex-direction: column; justify-content: space-between; padding: 0 0 4px; color: var(--muted); font-size: 10px; text-align: right; }
.trend-plot { position: relative; display: flex; align-items: stretch; min-width: 0; padding: 0 5px; border-left: 1px solid var(--line); border-bottom: 1px solid var(--line); }
.trend-grid-line { position: absolute; inset: 0 0 auto; height: 1px; background: #e5ebf0; }
.trend-grid-line:nth-child(2) { top: 50%; }
.trend-grid-line:nth-child(3) { top: 100%; }
.trend-bars { position: relative; z-index: 1; display: flex; align-items: flex-end; gap: 2px; justify-content: space-between; width: 100%; height: 100%; }
.trend-bar { flex: 1 1 0; min-width: 2px; min-height: 2px; max-width: 14px; opacity: .75; border-radius: 3px 3px 0 0; }
.trend-axis { display: flex; justify-content: space-between; margin: 8px 0 0 98px; color: var(--muted); font-size: 10px; }
.history-more { margin-top: 14px; }
.history-record-metrics { display: flex; flex-wrap: wrap; gap: 10px; margin-top: 7px; color: var(--muted); font-size: 11px; }
.history-record-metrics b { color: var(--ink); font-weight: 600; }
.history-view .timeline article { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
@media (max-width: 1000px) { .history-summary-grid { grid-template-columns: repeat(2, 1fr); } .history-summary-grid article:nth-child(2n) { border-right: 0; } .history-summary-grid article:nth-child(-n+2) { border-bottom: 1px solid var(--line); } }
@media (max-width: 700px) { .history-metric-tabs { width: 100%; overflow-x: auto; } .section-title { flex-wrap: wrap; } .trend-chart { grid-template-columns: 62px minmax(0, 1fr); } .trend-axis { margin-left: 74px; } }
@media (max-width: 540px) { .history-summary-grid { grid-template-columns: 1fr; } .history-summary-grid article { border-right: 0; border-bottom: 1px solid var(--line); } .history-summary-grid article:last-child { border-bottom: 0; } }
</style>
