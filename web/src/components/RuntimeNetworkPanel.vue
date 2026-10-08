<script setup lang="ts">
import { computed, ref } from "vue";
import StatusBadge from "@/components/StatusBadge.vue";
import type { RealtimeNetworkResult, RuntimeNetworkNode } from "@/types";

const props = defineProps<{ network: RealtimeNetworkResult; tick: number }>();
const system = ref("all");
const labels: Record<string, string> = {
  chilled_water: "冷冻水", cooling_water: "冷却水", hot_water: "热水", air: "空气",
};
const entries = computed(() => [...props.network.pipes, ...props.network.ducts]
  .filter((node) => system.value === "all" || node.system === system.value));
const circuits = computed(() => props.network.circuits
  .filter((circuit) => system.value === "all" || circuit.system === system.value));
function number(value: number | null, digits = 3): string {
  return value !== null && Number.isFinite(value) ? value.toFixed(digits) : "--";
}
function state(node: RuntimeNetworkNode): string {
  if (node.model_status === "unsupported") return "接法暂不支持";
  if (node.model_status === "disconnected") return "通路未完整";
  if (!props.tick) return "待运行";
  return node.flow_blocked ? "已断流" : "通流";
}
function sourceLabel(value: string): string {
  if (value.startsWith("input:")) return "项目输入";
  if (value.startsWith("catalog:")) return "设备目录";
  if (value.startsWith("rules:")) return "教学默认参数";
  return "计算推导";
}
</script>

<template>
  <section class="band-section network-panel">
    <div class="section-title">
      <div><h2>管路实时读数</h2><p class="editor-note">水路、风路和设备共用本次计算的流量。每个独立子系统分别计算。</p></div>
      <span>计算步 {{ tick }}</span>
    </div>
    <div class="network-content">
      <label class="network-filter">查看系统
        <select v-model="system" aria-label="运行网络系统">
          <option value="all">全部系统</option>
          <option v-for="(label, key) in labels" :key="key" :value="key">{{ label }}</option>
        </select>
      </label>
      <div class="network-circuits">
        <article v-for="circuit in circuits" :key="circuit.circuit_id" class="network-circuit">
          <strong>{{ labels[circuit.system] }} · {{ circuit.source_ids.join('、') || '未连接冷热源' }}</strong>
          <span v-if="circuit.model_status === 'unsupported'">不可计算</span>
          <span v-else>{{ number(circuit.flow_m3_h) }} m³/h</span>
          <small v-if="circuit.model_status !== 'unsupported' && circuit.supply_temp_c !== null">供给 / 回流温度：{{ number(circuit.supply_temp_c, 2) }} / {{ number(circuit.return_temp_c, 2) }} ℃</small>
          <StatusBadge v-if="circuit.model_status !== 'modeled'" tone="warn" :label="circuit.model_status === 'unsupported' ? '接法暂不支持' : '通路未完整'" />
        </article>
      </div>
      <div v-if="entries.length" class="table-wrap">
        <table>
          <thead><tr><th>管段 / 风管</th><th>系统</th><th>状态</th><th>流量（m³/h）</th><th>流速（m/s）</th><th>压降（Pa）</th><th>参数来源</th></tr></thead>
          <tbody>
            <tr v-for="node in entries" :key="`${node.system}:${node.node_id}`">
              <td><strong>{{ node.node_id }}</strong></td><td>{{ labels[node.system] }}</td>
              <td><StatusBadge :tone="node.model_status === 'modeled' ? node.flow_blocked ? 'muted' : 'ok' : 'warn'" :label="state(node)" /></td>
              <td>{{ node.model_status === 'unsupported' ? '不可计算' : number(node.flow_m3_h) }}</td>
              <td>{{ node.model_status === 'unsupported' ? '--' : number(node.velocity_m_s) }}</td>
              <td>{{ node.model_status === 'unsupported' ? '--' : number(node.pressure_drop_pa, 1) }}</td>
              <td>{{ [...new Set(node.parameter_sources.map(sourceLabel))].join('、') || '--' }}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <p v-else class="empty-state">此系统没有管段或风管。</p>
      <ul v-if="network.issues.length" class="network-issues">
        <li v-for="(issue, index) in network.issues" :key="`${issue.code}:${issue.target}:${index}`"><strong v-if="issue.target">{{ issue.target }}：</strong>{{ issue.message }}</li>
      </ul>
      <p class="table-note">采用可用流量、扬程 / 静压和通路阻力约束的教学模型。缺少厂家性能曲线的部分使用已标注来源的近似参数。</p>
    </div>
  </section>
</template>

<style scoped>
.network-content { padding: 1rem 1.25rem; }
.network-filter { display: flex; align-items: center; gap: .75rem; margin-bottom: 1rem; }
.network-circuits { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: .75rem; margin-bottom: 1.25rem; }
.network-circuit { display: flex; flex-direction: column; align-items: flex-start; gap: .4rem; padding: .9rem; border: 1px solid var(--border, #dce3df); border-radius: .5rem; }
.network-circuit strong { overflow-wrap: anywhere; }
.network-issues { padding-left: 1.5rem; font-size: .85rem; line-height: 1.7; }
.table-note { margin-top: 1rem; font-size: .8rem; line-height: 1.6; }
</style>
