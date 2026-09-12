<script setup lang="ts">
import { computed, ref } from "vue";
import { Bolt, Building, Droplets, GitBranch, Layers, Play, RefreshCw, Thermometer } from "lucide-vue-next";
import StatusBadge from "@/components/StatusBadge.vue";
import { useEvaluationStore } from "@/stores/evaluation";
import { useProjectStore } from "@/stores/project";
import { finite, valueWithUnit } from "@/utils/format";
import { HVAC_SYSTEMS, SYSTEM_LABELS } from "@/utils/project-editor";
import type { HydraulicNetworkResult, PipeCalculation } from "@/types";

const project = useProjectStore();
const evaluation = useEvaluationStore();
const energyHours = ref(24);
const energyPrice = ref(0.85);
const edgeCount = computed(() => Object.values(evaluation.result?.topology?.edge_count_by_system ?? {}).reduce((sum, value) => sum + value, 0));

// 面积指标已不是分项，它是校核值（index_estimate_kw），不进这张分项表。
const COMPONENT_LABELS: Record<string, string> = {
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
const componentOrder = [
  "people", "lighting", "equipment",
  "fresh_air_cooling", "latent_cooling", "solar",
  "envelope_cooling", "envelope_heating", "fresh_air_heating",
];
const componentKeys = computed(() => {
  const seen = new Set<string>();
  for (const room of evaluation.result?.load?.rooms ?? []) {
    for (const key of Object.keys(room.components_kw ?? {})) seen.add(key);
  }
  return componentOrder.filter((key) => seen.has(key));
});

function topRoomName(field: "cooling_kw" | "heating_kw"): string {
  const rooms = evaluation.result?.load?.rooms ?? [];
  if (!rooms.length) return "";
  return [...rooms].sort((a, b) => (b[field] ?? 0) - (a[field] ?? 0))[0]?.name ?? "";
}

const hydraulic = computed<HydraulicNetworkResult | null>(() => evaluation.result?.hydraulic_network ?? null);
const hydraulicSystemFilter = ref<string>("all");
const hydraulicSystems = computed(() => {
  const seen = new Set<string>(hydraulic.value?.pipes.map((pipe) => pipe.system) ?? []);
  return HVAC_SYSTEMS.filter((system) => seen.has(system));
});
const filteredPipes = computed<PipeCalculation[]>(() => {
  const pipes = hydraulic.value?.pipes ?? [];
  return hydraulicSystemFilter.value === "all" ? pipes : pipes.filter((pipe) => pipe.system === hydraulicSystemFilter.value);
});
const mostUnfavorablePipe = computed(() =>
  hydraulic.value?.pipes.find((pipe) => pipe.pipe_id === hydraulic.value?.most_unfavorable_pipe_id) ?? null,
);

function pipeSizeLabel(size: Record<string, unknown>): string {
  for (const key of ["selected_size", "size", "dn", "diameter"]) {
    const value = size[key];
    if (typeof value === "string" && value.trim()) return value;
    if (typeof value === "number" && Number.isFinite(value)) return `DN${Math.round(value)}`;
  }
  return "--";
}

function marginLabel(value: number | null | undefined): string {
  return typeof value === "number" && Number.isFinite(value) ? `${finite(value, 1)}x` : "--";
}

async function run() {
  if (project.project && project.projectId) await evaluation.evaluate(project.projectId, project.project);
}
</script>

<template>
  <div class="view-stack">
    <section class="view-heading action-heading">
      <div><p class="eyebrow">SYSTEM EVALUATION</p><h1>系统评估</h1><p>负荷、设备、水力与拓扑联合计算</p></div>
      <div class="toolbar"><button class="button primary" :disabled="evaluation.state === 'loading' || !project.project" @click="run"><RefreshCw :class="{ spin: evaluation.state === 'loading' }" />运行评估</button></div>
    </section>
    <div v-if="evaluation.error" class="error-banner">{{ evaluation.error }}</div>

    <section v-if="evaluation.result" class="metric-grid">
      <article class="metric-cell"><Building /><span>设计冷负荷</span><strong>{{ valueWithUnit(evaluation.result.load?.adjusted_cooling_kw, "kW") }}</strong></article>
      <article class="metric-cell"><GitBranch /><span>拓扑规模</span><strong>{{ evaluation.result.topology?.node_count ?? 0 }} 节点</strong><small>{{ edgeCount }} 条连接</small></article>
      <article class="metric-cell"><Bolt /><span>泵扬程</span><strong>{{ valueWithUnit(evaluation.result.water?.pump_head_m, "m") }}</strong></article>
    </section>

    <section v-if="evaluation.result?.load" class="split-band">
      <div class="band-section">
        <div class="section-title"><div><p class="eyebrow">TOTAL LOAD</p><h2>总负荷</h2></div><Building /></div>
        <div class="kv-grid">
          <div><span>总冷负荷</span><strong>{{ valueWithUnit(evaluation.result.load.total_cooling_kw, "kW") }}</strong></div>
          <div><span>总热负荷</span><strong>{{ valueWithUnit(evaluation.result.load.total_heating_kw, "kW") }}</strong></div>
          <div><span>修正冷负荷</span><strong>{{ valueWithUnit(evaluation.result.load.adjusted_cooling_kw, "kW") }}</strong></div>
          <div><span>修正热负荷</span><strong>{{ valueWithUnit(evaluation.result.load.adjusted_heating_kw, "kW") }}</strong></div>
        </div>
      </div>
      <div class="band-section">
        <div class="section-title"><div><p class="eyebrow">ROOMS</p><h2>房间负荷</h2></div><Layers /></div>
        <div class="kv-grid">
          <div><span>计算房间</span><strong>{{ evaluation.result.load.rooms.length }} 间</strong></div>
          <div><span>参与楼层</span><strong>{{ evaluation.result.load.floor_loads.length }} 层</strong></div>
          <div><span>冷负荷最大房间</span><strong>{{ topRoomName("cooling_kw") || "--" }}</strong></div>
          <div><span>热负荷最大房间</span><strong>{{ topRoomName("heating_kw") || "--" }}</strong></div>
        </div>
      </div>
    </section>

    <section v-if="evaluation.result?.load?.rooms?.length" class="band-section">
      <div class="section-title"><div><p class="eyebrow">ROOM DETAILS</p><h2>房间负荷明细</h2></div><Thermometer /></div>
      <div class="table-wrap"><table><thead><tr><th>房间</th><th>楼层</th><th v-for="key in componentKeys" :key="key">{{ COMPONENT_LABELS[key] ?? key }}</th><th>冷负荷</th><th>热负荷</th></tr></thead><tbody>
        <tr v-for="room in evaluation.result.load.rooms" :key="room.name || room.id || room.floor_id"><td><strong>{{ room.name }}</strong></td><td>{{ room.floor_id }}</td><td v-for="key in componentKeys" :key="key">{{ finite(room.components_kw?.[key] ?? 0, 2) }}</td><td><strong>{{ finite(room.cooling_kw, 2) }} kW</strong></td><td><strong>{{ finite(room.heating_kw, 2) }} kW</strong></td></tr>
      </tbody></table></div>
      <p class="table-note">分项单位 kW；冷负荷 = 面积指标 + 人员 + 照明 + 设备 + 新风显热 + 新风潜热 + 太阳辐射，热负荷取「面积指标」与「围护 + 新风加热」较大值。</p>
    </section>

    <section v-if="evaluation.result" class="band-section">
      <div class="section-title"><h2>关键结果</h2></div>
      <div class="kv-grid">
        <div><span>设计热负荷</span><strong>{{ valueWithUnit(evaluation.result.load?.adjusted_heating_kw, "kW") }}</strong></div>
        <div><span>水流量</span><strong>{{ valueWithUnit(evaluation.result.water?.flow_m3_h, "m³/h") }}</strong></div>
        <div><span>水压降</span><strong>{{ valueWithUnit(evaluation.result.water?.pressure_drop_pa, "Pa", 0) }}</strong></div>
        <div><span>风量</span><strong>{{ valueWithUnit(evaluation.result.air?.airflow_m3_h, "m³/h", 0) }}</strong></div>
      </div>
    </section>

    <section v-if="evaluation.result" class="band-section">
      <div class="section-title"><div><p class="eyebrow">FLOORS</p><h2>楼层负荷</h2></div><Building /></div>
      <div class="table-wrap"><table><thead><tr><th>楼层</th><th>面积</th><th>房间</th><th>人数</th><th>冷负荷</th><th>热负荷</th></tr></thead><tbody>
        <tr v-for="floor in evaluation.result.load?.floor_loads ?? []" :key="floor.floor_id"><td><strong>{{ floor.floor_name }}</strong><small>{{ floor.floor_id }}</small></td><td>{{ finite(floor.area_m2, 0) }} m²</td><td>{{ floor.room_count }}</td><td>{{ floor.people }}</td><td>{{ finite(floor.adjusted_cooling_kw) }} kW</td><td>{{ finite(floor.adjusted_heating_kw) }} kW</td></tr>
      </tbody></table></div>
    </section>

    <section v-if="evaluation.result?.topology" class="band-section">
      <div class="section-title"><div><p class="eyebrow">TOPOLOGY</p><h2>关键路径</h2></div><GitBranch /></div>
      <div class="path-list"><article v-for="path in evaluation.result.topology.paths" :key="path.name"><StatusBadge :tone="path.complete ? 'ok' : 'error'" :label="path.complete ? '完整' : '断开'" /><div><strong>{{ path.name }}</strong><p>{{ path.nodes.join(' → ') || '没有可用节点' }}</p></div></article></div>
    </section>

    <section v-if="evaluation.result && !hydraulic" class="band-section">
      <div class="section-title"><div><p class="eyebrow">HYDRAULIC</p><h2>水力校核</h2></div><Droplets /></div>
      <div class="empty-state">暂无水力校核数据。冷负荷为 0 或未连接水系统时不会生成水力计算。</div>
    </section>

    <section v-if="hydraulic" class="band-section">
      <div class="section-title"><div><p class="eyebrow">HYDRAULIC</p><h2>水力校核</h2></div><Droplets /></div>

      <div class="hydraulic-summary">
        <article class="metric-cell"><span>所需泵扬程</span><strong>{{ valueWithUnit(hydraulic.required_pump_head_m, "m") }}</strong><small v-if="mostUnfavorablePipe">最不利管段 {{ mostUnfavorablePipe.pipe_id }}</small></article>
        <article class="metric-cell">
          <span>流量余量</span><strong>{{ marginLabel(hydraulic.pump_check?.flow_margin) }}</strong>
          <small v-if="hydraulic.pump_check">{{ finite(hydraulic.pump_check.required_flow_m3_h) }} / {{ finite(hydraulic.pump_check.installed_flow_m3_h) }} m³/h</small>
          <small v-else>需装流量未知</small>
        </article>
        <article class="metric-cell">
          <span>扬程余量</span><strong>{{ marginLabel(hydraulic.pump_check?.head_margin) }}</strong>
          <small v-if="hydraulic.pump_check">{{ finite(hydraulic.pump_check.required_head_m) }} / {{ finite(hydraulic.pump_check.available_head_m) }} m</small>
          <small v-else>需可用扬程未知</small>
        </article>
        <article v-if="hydraulic.pump_check" class="metric-cell pump-status"><span>泵校核</span><StatusBadge :tone="hydraulic.pump_check.status === 'ok' ? 'ok' : 'warn'" :label="hydraulic.pump_check.status === 'ok' ? '合格' : '告警'" /><small>{{ hydraulic.pump_check.duty_pump_count }} 用 {{ hydraulic.pump_check.standby_pump_count }} 备</small></article>
      </div>

      <div class="hydraulic-toolbar">
        <div class="flow-chips">
          <span v-for="(flow, system) in hydraulic.total_flow_by_system_m3_h" :key="system" class="flow-chip">{{ SYSTEM_LABELS[system] ?? system }} {{ finite(flow, 1) }} m³/h</span>
        </div>
        <label>系统筛选<select v-model="hydraulicSystemFilter"><option value="all">全部（{{ hydraulic.pipes.length }} 段）</option><option v-for="system in hydraulicSystems" :key="system" :value="system">{{ SYSTEM_LABELS[system] ?? system }}（{{ hydraulic.pipes.filter((pipe) => pipe.system === system).length }} 段）</option></select></label>
      </div>

      <div class="table-wrap"><table><thead><tr><th>序号</th><th>管段</th><th>系统</th><th>规格</th><th>流量</th><th>流速</th><th>压降</th><th>沿程扬程</th></tr></thead><tbody>
        <tr v-for="(pipe, index) in filteredPipes" :key="pipe.pipe_id" :class="{ 'pipe-unfavorable': pipe.pipe_id === hydraulic.most_unfavorable_pipe_id }">
          <td class="pipe-index">{{ index + 1 }}</td>
          <td><strong>{{ pipe.pipe_id }}</strong><StatusBadge v-if="pipe.pipe_id === hydraulic.most_unfavorable_pipe_id" tone="warn" label="最不利" /></td>
          <td>{{ SYSTEM_LABELS[pipe.system] ?? pipe.system }}</td>
          <td>{{ pipeSizeLabel(pipe.size) }}</td>
          <td>{{ finite(pipe.flow_m3_h, 2) }} m³/h</td>
          <td>{{ finite(pipe.velocity_m_s, 2) }} m/s</td>
          <td>{{ finite(pipe.pressure_drop_pa, 0) }} Pa</td>
          <td>{{ pipe.pump_head_m == null ? "--" : `${finite(pipe.pump_head_m, 2)} m` }}</td>
        </tr>
      </tbody></table></div>

      <div v-if="hydraulic.paths && hydraulic.paths.length > 0" class="table-wrap paths-table">
        <h3>环路路径</h3>
        <table><thead><tr><th>路径</th><th>系统</th><th>闭合环路</th><th>管段数</th><th>压降</th><th>扬程</th></tr></thead><tbody>
          <tr v-for="path in hydraulic.paths" :key="path.path_id" :class="{ 'path-unfavorable': path.path_id === hydraulic.most_unfavorable_path_id }">
            <td><strong>{{ path.path_id }}</strong><StatusBadge v-if="path.path_id === hydraulic.most_unfavorable_path_id" tone="warn" label="最不利" /></td>
            <td>{{ SYSTEM_LABELS[path.system] ?? path.system }}</td>
            <td>{{ path.closed_circuit ? "是" : "否" }}</td>
            <td>{{ path.pipe_ids.length }}</td>
            <td>{{ finite(path.pressure_drop_pa, 0) }} Pa</td>
            <td>{{ path.head_m == null ? "--" : `${finite(path.head_m, 2)} m` }}</td>
          </tr>
        </tbody></table>
      </div>

      <p class="table-note">沿程扬程为空表示该管段不在最不利路径上；闭合环路包含供程与回程，是水泵扬程的真实依据；水力告警已并入下方问题列表。</p>
    </section>

    <section v-if="evaluation.result?.simulation" class="band-section">
      <div class="section-title"><div><p class="eyebrow">STEADY STATE</p><h2>稳态仿真</h2></div><Thermometer /></div>
      <div class="kv-grid">
        <div><span>供水温度</span><strong>{{ valueWithUnit(evaluation.result.simulation.supply_water_temp_c, "°C") }}</strong></div>
        <div><span>回水温度</span><strong>{{ valueWithUnit(evaluation.result.simulation.return_water_temp_c, "°C") }}</strong></div>
        <div><span>送风温度</span><strong>{{ valueWithUnit(evaluation.result.simulation.supply_air_temp_c, "°C") }}</strong></div>
        <div><span>估算房间温度</span><strong>{{ valueWithUnit(evaluation.result.simulation.estimated_room_temp_c, "°C") }}</strong></div>
        <div><span>制冷量</span><strong>{{ valueWithUnit(evaluation.result.simulation.cooling_energy_kw, "kW") }}</strong></div>
        <div><span>泵功耗</span><strong>{{ valueWithUnit(evaluation.result.simulation.pump_energy_kw, "kW") }}</strong></div>
        <div><span>风机功耗</span><strong>{{ valueWithUnit(evaluation.result.simulation.fan_energy_kw, "kW") }}</strong></div>
        <div><span>总能耗</span><strong>{{ valueWithUnit(evaluation.result.simulation.total_energy_kw, "kW") }}</strong></div>
      </div>
    </section>

    <section v-if="evaluation.result" class="band-section">
      <div class="section-title"><div><p class="eyebrow">ISSUES</p><h2>问题与建议</h2></div></div>
      <div class="issue-list">
        <article v-for="issue in evaluation.result.issues" :key="`${issue.code}-${issue.target}`" class="issue-row" :class="`issue-${issue.level}`"><StatusBadge :tone="issue.level === 'error' ? 'error' : issue.level === 'warning' ? 'warn' : 'muted'" :label="issue.code" /><div><strong>{{ issue.message }}</strong><small>{{ issue.target || '系统' }}</small></div></article>
        <article v-for="recommendation in evaluation.result.recommendations" :key="recommendation" class="issue-row recommendation"><StatusBadge tone="ok" label="建议" /><strong>{{ recommendation }}</strong></article>
      </div>
    </section>

    <section class="split-band tools-band">
      <div class="band-section">
        <div class="section-title"><h2>能耗仿真</h2></div>
        <div class="form-grid"><label>仿真小时<input v-model.number="energyHours" type="number" min="1" max="8760" /></label><label>电价（元/kWh）<input v-model.number="energyPrice" type="number" min="0" step="0.01" /></label></div>
        <button class="button secondary" :disabled="!project.project" @click="project.project && evaluation.calculateEnergy(project.project, energyHours, energyPrice)"><Play />计算能耗</button>
        <div v-if="evaluation.energy" class="inline-results"><div><span>总能耗</span><strong>{{ finite(evaluation.energy.total_energy_kwh) }} kWh</strong></div><div><span>预计费用</span><strong>¥ {{ finite(evaluation.energy.estimated_cost) }}</strong></div></div>
      </div>
    </section>

    <section v-if="!evaluation.result" class="empty-state large">尚未执行评估。选择项目后点击“运行评估”。</section>
  </div>
</template>
