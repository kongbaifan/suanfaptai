<script setup lang="ts">
import { computed, onMounted } from "vue";
import { Clock3, History, RefreshCw } from "lucide-vue-next";
import { useEvaluationStore } from "@/stores/evaluation";
import { useProjectStore } from "@/stores/project";
import { useRealtimeStore } from "@/stores/realtime";
import { dateTime, finite } from "@/utils/format";

const project = useProjectStore();
const evaluation = useEvaluationStore();
const realtime = useRealtimeStore();

async function refresh() {
  if (project.projectId && project.projectId !== "sample") await Promise.all([evaluation.loadHistory(project.projectId), realtime.loadHistory(project.projectId)]);
}
onMounted(refresh);
</script>

<template>
  <div class="view-stack">
    <section class="view-heading action-heading"><div><p class="eyebrow">HISTORY</p><h1>历史趋势</h1><p>评估记录与运行快照</p></div><button class="icon-button" title="刷新历史" @click="refresh"><RefreshCw /></button></section>
    <section class="split-band">
      <div class="band-section"><div class="section-title"><div><p class="eyebrow">EVALUATIONS</p><h2>评估历史</h2></div><History /></div>
        <div v-if="evaluation.history.length" class="timeline"><article v-for="item in evaluation.history" :key="item.history_id"><div><strong>{{ dateTime(item.created_at) }}</strong><small>{{ item.issue_count }} 个问题 · {{ item.history_id }}</small></div></article></div>
        <div v-else class="empty-state">该项目还没有评估历史。</div>
      </div>
      <div class="band-section"><div class="section-title"><div><p class="eyebrow">RUNTIME</p><h2>运行采样</h2></div><Clock3 /></div><div class="metric-grid compact-metrics"><article class="metric-cell"><span>保存点</span><strong>{{ realtime.history.length }}</strong></article><article class="metric-cell"><span>当前告警</span><strong>{{ realtime.current?.alarms.length ?? 0 }}</strong></article></div></div>
    </section>
    <section class="band-section"><div class="section-title"><h2>最近运行快照</h2></div><div class="table-wrap"><table><thead><tr><th>时间 / Tick</th><th>模式</th><th>设备数</th><th>告警</th><th>总功率</th></tr></thead><tbody><tr v-for="(point, index) in realtime.history.slice(-50).reverse()" :key="index"><td>{{ dateTime(point.updated_at ?? point.timestamp) }}</td><td>{{ point.mode ?? (point.summary as Record<string, unknown> | undefined)?.mode ?? '--' }}</td><td>{{ Object.keys((point.devices as Record<string, unknown>) ?? {}).length }}</td><td>{{ Array.isArray(point.alarms) ? point.alarms.length : '--' }}</td><td>{{ finite((point.summary as Record<string, unknown> | undefined)?.total_power_kw) }} kW</td></tr></tbody></table></div><div v-if="!realtime.history.length" class="empty-state">启动一次仿真后会在这里显示运行历史。</div></section>
  </div>
</template>
