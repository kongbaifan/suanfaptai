<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from "vue";
import { ChevronRight, Download, Play, RefreshCw } from "lucide-vue-next";
import StatusBadge from "@/components/StatusBadge.vue";
import { api, errorMessage } from "@/api/client";
import { useEvaluationStore } from "@/stores/evaluation";
import { useProjectStore } from "@/stores/project";
import { finite, valueWithUnit } from "@/utils/format";
import { HVAC_SYSTEMS, SYSTEM_LABELS } from "@/utils/project-editor";
import type { HydraulicNetworkResult, Issue, PipeCalculation } from "@/types";
import type { AirNetworkResult, EvaluationReport } from "@/types/evaluation";

const project = useProjectStore();
const evaluation = useEvaluationStore();
const tabs = ["summary", "load", "water", "air", "energy"] as const;
const labels: Record<(typeof tabs)[number], string> = {
  summary: "评估结论",
  load: "建筑负荷",
  water: "水系统",
  air: "风系统",
  energy: "稳态与能耗",
};
const activeTab = ref<(typeof tabs)[number]>("summary");
const issueFilter = ref("all");
const hydraulicSystemFilter = ref("all");
const energyHours = ref(24);
const energyPrice = ref(0.85);
const energyLoading = ref(false);
const reportLoading = ref(false);
const reportError = ref("");
const reportNotice = ref("");
let exportRevision = 0;
let energyRevision = 0;
const edgeCount = computed(() =>
  Object.values(evaluation.result?.topology?.edge_count_by_system ?? {}).reduce(
    (sum, value) => sum + value,
    0,
  ),
);
const issues = computed(() => evaluation.result?.issues ?? []);
const counts = computed(() => ({
  error: issues.value.filter((x) => x.level === "error").length,
  warning: issues.value.filter((x) => x.level === "warning").length,
  info: issues.value.filter((x) => x.level === "info").length,
}));
const filteredIssues = computed(() =>
  issues.value.filter(
    (x) => issueFilter.value === "all" || x.level === issueFilter.value,
  ),
);
const hydraulic = computed<HydraulicNetworkResult | null>(
  () => evaluation.result?.hydraulic_network ?? null,
);
const air = computed(
  () => evaluation.result?.air_network as AirNetworkResult | null | undefined,
);
const hydraulicSystems = computed(() =>
  HVAC_SYSTEMS.filter((s) =>
    hydraulic.value?.pipes.some((p) => p.system === s),
  ),
);
const filteredPipes = computed<PipeCalculation[]>(() =>
  (hydraulic.value?.pipes ?? []).filter(
    (p) =>
      hydraulicSystemFilter.value === "all" ||
      p.system === hydraulicSystemFilter.value,
  ),
);
const energyValid = computed(
  () =>
    Number.isInteger(energyHours.value) &&
    energyHours.value >= 1 &&
    energyHours.value <= 8760 &&
    Number.isFinite(energyPrice.value) &&
    energyPrice.value >= 0,
);
const componentLabels: Record<string, string> = {
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
const componentKeys = computed(() => {
  const keys = new Set(
    (evaluation.result?.load?.rooms ?? []).flatMap((room) =>
      Object.keys(room.components_kw ?? {}),
    ),
  );
  return [
    "people",
    "lighting",
    "equipment",
    "fresh_air_cooling",
    "latent_cooling",
    "solar",
    "envelope_cooling",
    "envelope_heating",
    "fresh_air_heating",
  ].filter((key) => keys.has(key));
});

/**
 * 房间负荷明细的两级结构：**楼层 → 房间**。
 *
 * 楼层行显示该层**合计**（各分项直接相加，与房间行同口径、都是未乘系数的值），
 * 房间行显示逐房间值。楼层顺序沿用后端 `floor_loads`（已按 level→id 排序），
 * 不在 `floor_loads` 里的楼层（理论不该出现）追加在末尾，保证房间不会丢。
 *
 * 未修正 vs 修正：本表用的是未乘同时/安全系数的值（可直接相加），
 * 上面「楼层负荷」表用的是 `adjusted_*`（乘过 0.9×1.1）——两张表口径不同，
 * 不要互相核对。
 */
interface RoomLoadRow {
  key: string;
  name: string;
  floorId: string;
  components: Record<string, number>;
  coolingKw: number;
  heatingKw: number;
}

interface FloorLoadGroup {
  floorId: string;
  floorName: string;
  roomCount: number;
  people: number;
  areaM2: number;
  totals: Record<string, number>;
  coolingKw: number;
  heatingKw: number;
  rooms: RoomLoadRow[];
}

const collapsedFloorIds = ref<Set<string>>(new Set());

function toggleFloorGroup(floorId: string) {
  const next = new Set(collapsedFloorIds.value);
  if (next.has(floorId)) next.delete(floorId);
  else next.add(floorId);
  collapsedFloorIds.value = next;
}

const roomLoadTree = computed<FloorLoadGroup[]>(() => {
  const rooms = evaluation.result?.load?.rooms ?? [];
  const floors = evaluation.result?.load?.floor_loads ?? [];
  const byFloor = new Map<string, RoomLoadRow[]>();
  rooms.forEach((room, index) => {
    const floorId = room.floor_id || "未指定楼层";
    const rows = byFloor.get(floorId) ?? [];
    rows.push({
      // 房间可能没有 id、也允许重名，必须带上序号，否则 key 会重复。
      key: `${room.id ?? "room"}-${index}`,
      name: room.name,
      floorId: room.floor_id,
      components: room.components_kw ?? {},
      coolingKw: room.cooling_kw,
      heatingKw: room.heating_kw,
    });
    byFloor.set(floorId, rows);
  });

  const orderedFloorIds = [
    ...floors.map((floor) => floor.floor_id),
    ...[...byFloor.keys()].filter(
      (floorId) => !floors.some((floor) => floor.floor_id === floorId),
    ),
  ];

  return orderedFloorIds
    .filter((floorId) => byFloor.has(floorId))
    .map((floorId) => {
      const rows = byFloor.get(floorId) ?? [];
      const meta = floors.find((floor) => floor.floor_id === floorId);
      const totals: Record<string, number> = {};
      for (const key of componentKeys.value) {
        totals[key] = rows.reduce(
          (sum, row) => sum + (row.components[key] ?? 0),
          0,
        );
      }
      return {
        floorId,
        floorName: meta?.floor_name || floorId,
        roomCount: meta?.room_count ?? rows.length,
        people: meta?.people ?? 0,
        areaM2: meta?.area_m2 ?? 0,
        totals,
        coolingKw: rows.reduce((sum, row) => sum + row.coolingKw, 0),
        heatingKw: rows.reduce((sum, row) => sum + row.heatingKw, 0),
        rooms: rows,
      };
    });
});

function topRoom(field: "cooling_kw" | "heating_kw") {
  return (
    [...(evaluation.result?.load?.rooms ?? [])].sort(
      (a, b) => b[field] - a[field],
    )[0]?.name ?? "--"
  );
}
function issueTone(level: Issue["level"]) {
  return level === "error" ? "error" : level === "warning" ? "warn" : "muted";
}
function margin(value: number | null | undefined) {
  return typeof value === "number" && Number.isFinite(value)
    ? `${finite(value, 1)}x`
    : "--";
}
function sizeLabel(size: Record<string, unknown>) {
  const value = size.selected_size ?? size.size ?? size.dn ?? size.diameter;
  return value
    ? typeof value === "number"
      ? `DN${Math.round(value)}`
      : String(value)
    : size.width_mm && size.height_mm
      ? `${size.width_mm} × ${size.height_mm} mm`
      : "--";
}
async function run() {
  if (project.project && project.projectId) {
    activeTab.value = "summary";
    await evaluation.evaluate(project.projectId, project.project);
  }
}
async function energy() {
  if (!project.project || !energyValid.value || energyLoading.value) return;
  const revision = ++energyRevision;
  energyLoading.value = true;
  try {
    await evaluation.calculateEnergy(
      project.project,
      energyHours.value,
      energyPrice.value,
    );
  } finally {
    if (revision === energyRevision) energyLoading.value = false;
  }
}
async function downloadReport() {
  if (!project.project || reportLoading.value) return;
  const selection = project.selectionKey;
  const revision = ++exportRevision;
  const projectBody = JSON.stringify(project.project);
  reportLoading.value = true;
  reportError.value = "";
  reportNotice.value = "";
  try {
    const report = await api<EvaluationReport>("/api/v1/report/download", {
      method: "POST",
      body: projectBody,
    });
    if (project.selectionKey !== selection || revision !== exportRevision)
      return;
    const url = URL.createObjectURL(
      new Blob([JSON.stringify(report, null, 2)], {
        type: "application/json;charset=utf-8",
      }),
    );
    const link = document.createElement("a");
    try {
      link.href = url;
      link.download = "hvac_report.json";
      document.body.appendChild(link);
      link.click();
      reportNotice.value = "评估报告已导出。";
    } finally {
      link.remove();
      URL.revokeObjectURL(url);
    }
  } catch (error) {
    if (project.selectionKey === selection && revision === exportRevision)
      reportError.value = `报告导出失败：${errorMessage(error).replace(/^Error:\s*/, "")}`;
  } finally {
    if (revision === exportRevision) reportLoading.value = false;
  }
}
watch(
  () => project.selectionKey,
  () => {
    exportRevision += 1;
    energyRevision += 1;
    activeTab.value = "summary";
    issueFilter.value = "all";
    hydraulicSystemFilter.value = "all";
    reportLoading.value = false;
    energyLoading.value = false;
    reportError.value = "";
    reportNotice.value = "";
  },
);
onBeforeUnmount(() => {
  exportRevision += 1;
  energyRevision += 1;
});
</script>

<template>
  <div class="view-stack evaluation-workspace">
    <section class="view-heading action-heading">
      <div>
        <p class="eyebrow">DESIGN REVIEW</p>
        <h1>设计校核</h1>
        <p>先看结论与问题，再逐项检查负荷、管网和运行表现。</p>
      </div>
      <div class="toolbar">
        <button
          class="button secondary"
          :disabled="!project.project || reportLoading"
          @click="downloadReport"
        >
          <Download />{{ reportLoading ? "生成报告中…" : "导出报告" }}</button
        ><button
          class="button primary"
          :disabled="evaluation.state === 'loading' || !project.project"
          @click="run"
        >
          <RefreshCw />{{
            evaluation.state === "loading" ? "评估中…" : "运行评估"
          }}
        </button>
      </div>
    </section>
    <div v-if="evaluation.error" class="error-banner">
      {{ evaluation.error }}
    </div>
    <div v-if="reportError" class="error-banner">{{ reportError }}</div>
    <div v-if="reportNotice" class="notice-banner">{{ reportNotice }}</div>
    <div v-if="evaluation.state === 'loading'" class="notice-banner">
      正在计算负荷与各系统校核结果，请稍候。
    </div>
    <section v-if="evaluation.result" class="metric-grid">
      <article class="metric-cell">
        <span>设计冷负荷</span
        ><strong>{{
          valueWithUnit(evaluation.result.load?.adjusted_cooling_kw, "kW")
        }}</strong
        ><small>修正后</small>
      </article>
      <article class="metric-cell">
        <span>设计热负荷</span
        ><strong>{{
          valueWithUnit(evaluation.result.load?.adjusted_heating_kw, "kW")
        }}</strong
        ><small>{{ evaluation.result.load?.rooms.length ?? 0 }} 个房间</small>
      </article>
      <article class="metric-cell">
        <span>拓扑规模</span
        ><strong>{{ evaluation.result.topology?.node_count ?? 0 }} 节点</strong
        ><small>{{ edgeCount }} 条连接</small>
      </article>
      <article class="metric-cell">
        <span>待处理问题</span
        ><strong>{{ counts.error + counts.warning }} 项</strong
        ><small>{{ counts.error }} 错误 · {{ counts.warning }} 警告</small>
      </article>
    </section>
    <nav class="workspace-tabs" role="tablist" aria-label="设计校核内容">
      <button
        v-for="tab in tabs"
        :id="`evaluation-tab-${tab}`"
        :key="tab"
        role="tab"
        aria-controls="evaluation-panel"
        :aria-selected="activeTab === tab"
        :class="{ active: activeTab === tab }"
        @click="activeTab = tab"
      >
        {{ labels[tab] }}
      </button>
    </nav>
    <div
      id="evaluation-panel"
      class="view-stack"
      role="tabpanel"
      :aria-labelledby="`evaluation-tab-${activeTab}`"
    >
      <template v-if="activeTab === 'summary' && evaluation.result"
        ><section class="band-section review-conclusion">
          <div>
            <p class="eyebrow">REVIEW SUMMARY</p>
            <h2>
              {{
                counts.error
                  ? "先处理关键问题，再完善系统方案"
                  : counts.warning
                    ? "评估已完成，仍有校核项需要关注"
                    : "评估已完成，查看各系统计算结果"
              }}
            </h2>
            <p>
              校核包含建筑负荷、设备容量、连接关系和水风系统。导出报告可保存完整计算结果。
            </p>
          </div>
        </section>
        <section class="band-section">
          <div class="section-title">
            <h2>问题与建议</h2>
            <label
              >筛选<select v-model="issueFilter">
                <option value="all">全部（{{ issues.length }}）</option>
                <option value="error">错误（{{ counts.error }}）</option>
                <option value="warning">警告（{{ counts.warning }}）</option>
                <option value="info">提示（{{ counts.info }}）</option>
              </select></label
            >
          </div>
          <div v-if="filteredIssues.length" class="review-issues">
            <article
              v-for="(issue, index) in filteredIssues"
              :key="`${issue.code}-${index}`"
            >
              <StatusBadge
                :tone="issueTone(issue.level)"
                :label="
                  issue.level === 'error'
                    ? '错误'
                    : issue.level === 'warning'
                      ? '警告'
                      : '提示'
                "
              />
              <div>
                <strong>{{ issue.message }}</strong
                ><small
                  >{{ issue.target || "系统整体" }} · {{ issue.code }}</small
                >
              </div>
            </article>
          </div>
          <div v-else class="empty-state">
            {{ issues.length ? "当前等级没有问题。" : "本次评估未返回问题。" }}
          </div>
          <div
            v-if="evaluation.result.recommendations.length"
            class="recommendation-list"
          >
            <h3>改进建议</h3>
            <p v-for="item in evaluation.result.recommendations" :key="item">
              {{ item }}
            </p>
          </div>
        </section>
        <section class="band-section">
          <div class="section-title"><h2>关键结果</h2></div>
          <div class="kv-grid">
            <div>
              <span>设计水流量</span
              ><strong>{{
                valueWithUnit(evaluation.result.water?.flow_m3_h, "m³/h")
              }}</strong>
            </div>
            <div>
              <span>所需泵扬程</span
              ><strong>{{
                valueWithUnit(
                  hydraulic?.required_pump_head_m ??
                    evaluation.result.water?.pump_head_m,
                  "m",
                )
              }}</strong>
            </div>
            <div>
              <span>设计风量</span
              ><strong>{{
                valueWithUnit(evaluation.result.air?.airflow_m3_h, "m³/h", 0)
              }}</strong>
            </div>
            <div>
              <span>估算房间温度</span
              ><strong>{{
                valueWithUnit(
                  evaluation.result.simulation?.estimated_room_temp_c,
                  "°C",
                )
              }}</strong>
            </div>
          </div>
        </section></template
      >
      <section
        v-if="activeTab === 'summary' && evaluation.result"
        class="band-section"
      >
        <div class="section-title">
          <h2>关键路径</h2>
          <span>系统连接完整性</span>
        </div>
        <div
          v-if="evaluation.result.topology?.paths.length"
          class="review-paths"
        >
          <article
            v-for="path in evaluation.result.topology.paths"
            :key="`${path.system}-${path.name}`"
          >
            <StatusBadge
              :tone="path.complete ? 'ok' : 'error'"
              :label="path.complete ? '完整' : '断开'"
            />
            <div>
              <strong>{{ path.name }}</strong>
              <p>{{ path.nodes.join(" → ") || "没有可用节点" }}</p>
            </div>
          </article>
        </div>
        <div v-else class="empty-state">
          未生成可展示的系统路径，请检查设备与管段连接。
        </div>
      </section>
      <section
        v-if="activeTab === 'load' && evaluation.result?.load"
        class="band-section"
      >
        <div class="section-title"><h2>房间负荷概览</h2></div>
        <div class="kv-grid">
          <div>
            <span>计算房间</span
            ><strong>{{ evaluation.result.load.rooms.length }} 间</strong>
          </div>
          <div>
            <span>参与楼层</span
            ><strong>{{ evaluation.result.load.floor_loads.length }} 层</strong>
          </div>
          <div>
            <span>冷负荷最大房间</span
            ><strong>{{ topRoom("cooling_kw") }}</strong>
          </div>
          <div>
            <span>热负荷最大房间</span
            ><strong>{{ topRoom("heating_kw") }}</strong>
          </div>
        </div>
      </section>
      <section
        v-if="
          activeTab === 'load' && evaluation.result && !evaluation.result.load
        "
        class="band-section empty-state"
      >
        暂无建筑负荷结果，请添加房间并检查面积、人数和用途。
      </section>
      <template v-if="activeTab === 'load' && evaluation.result"
        ><section v-if="evaluation.result.load" class="band-section">
          <div class="section-title"><h2>总负荷</h2></div>
          <div class="kv-grid">
            <div>
              <span>总冷负荷</span
              ><strong>{{
                valueWithUnit(evaluation.result.load.total_cooling_kw, "kW")
              }}</strong>
            </div>
            <div>
              <span>总热负荷</span
              ><strong>{{
                valueWithUnit(evaluation.result.load.total_heating_kw, "kW")
              }}</strong>
            </div>
            <div>
              <span>修正冷负荷</span
              ><strong>{{
                valueWithUnit(evaluation.result.load.adjusted_cooling_kw, "kW")
              }}</strong>
            </div>
            <div>
              <span>修正热负荷</span
              ><strong>{{
                valueWithUnit(evaluation.result.load.adjusted_heating_kw, "kW")
              }}</strong>
            </div>
          </div>
        </section>
        <section v-if="evaluation.result.load" class="band-section">
          <div class="section-title"><h2>楼层负荷</h2></div>
          <div
            v-if="evaluation.result.load.floor_loads.length"
            class="table-wrap"
          >
            <table>
              <thead>
                <tr>
                  <th>楼层</th>
                  <th>面积</th>
                  <th>房间</th>
                  <th>人数</th>
                  <th>冷负荷</th>
                  <th>热负荷</th>
                </tr>
              </thead>
              <tbody>
                <tr
                  v-for="floor in evaluation.result.load.floor_loads"
                  :key="floor.floor_id"
                >
                  <td>{{ floor.floor_name }}</td>
                  <td>{{ finite(floor.area_m2, 0) }} m²</td>
                  <td>{{ floor.room_count }}</td>
                  <td>{{ floor.people }}</td>
                  <td>{{ finite(floor.adjusted_cooling_kw) }}</td>
                  <td>{{ finite(floor.adjusted_heating_kw) }}</td>
                </tr>
              </tbody>
            </table>
          </div>
          <div v-else class="empty-state">暂无楼层负荷汇总。</div>
        </section>
        <section v-if="evaluation.result.load" class="band-section">
          <div class="section-title"><h2>房间负荷明细</h2></div>
          <div v-if="evaluation.result.load.rooms.length" class="table-wrap">
            <table class="room-load-table">
              <thead>
                <tr>
                  <th>房间</th>
                  <th v-for="key in componentKeys" :key="key">
                    {{ componentLabels[key] }}
                  </th>
                  <th>冷负荷</th>
                  <th>热负荷</th>
                </tr>
              </thead>
              <tbody>
                <template v-for="group in roomLoadTree" :key="group.floorId">
                  <tr class="load-floor-row">
                    <td>
                      <div class="load-tree-cell">
                        <button
                          class="load-tree-expand"
                          type="button"
                          :aria-expanded="!collapsedFloorIds.has(group.floorId)"
                          :aria-label="`${group.floorName} 房间明细`"
                          @click="toggleFloorGroup(group.floorId)"
                        >
                          <ChevronRight class="load-tree-chevron" />
                        </button>
                        <strong>{{ group.floorName }}</strong>
                        <span class="load-tree-meta">
                          {{ group.roomCount }} 间 · {{ group.people }} 人 ·
                          {{ finite(group.areaM2, 0) }} m²
                        </span>
                      </div>
                    </td>
                    <td v-for="key in componentKeys" :key="key">
                      {{ finite(group.totals[key] ?? 0, 2) }}
                    </td>
                    <td>{{ finite(group.coolingKw, 2) }} kW</td>
                    <td>{{ finite(group.heatingKw, 2) }} kW</td>
                  </tr>
                  <tr
                    v-for="row in collapsedFloorIds.has(group.floorId)
                      ? []
                      : group.rooms"
                    :key="row.key"
                    class="load-room-row"
                  >
                    <td class="load-room-name">{{ row.name }}</td>
                    <td v-for="key in componentKeys" :key="key">
                      {{ finite(row.components[key] ?? 0, 2) }}
                    </td>
                    <td>{{ finite(row.coolingKw, 2) }} kW</td>
                    <td>{{ finite(row.heatingKw, 2) }} kW</td>
                  </tr>
                </template>
              </tbody>
            </table>
          </div>
          <div v-else class="empty-state">暂无房间负荷明细。</div>
          <p class="table-note">
            按楼层分组：楼层行为该层合计，点楼层行可展开/收起该层房间。分项单位为
            kW；冷负荷为人员、照明、设备、新风显热、新风潜热、太阳辐射与围护传热冷负荷之和；热负荷为围护传热与新风加热之和。面积指标仅用于校核，不计入分项合计。
          </p>
        </section></template
      >
      <section
        v-if="activeTab === 'water' && evaluation.result"
        class="band-section"
      >
        <div class="section-title"><h2>水系统设计参数</h2></div>
        <div class="kv-grid">
          <div>
            <span>设计水流量</span
            ><strong>{{
              valueWithUnit(evaluation.result.water?.flow_m3_h, "m³/h")
            }}</strong>
          </div>
          <div>
            <span>水压降</span
            ><strong>{{
              valueWithUnit(evaluation.result.water?.pressure_drop_pa, "Pa", 0)
            }}</strong>
          </div>
          <div>
            <span>基础估算泵扬程</span
            ><strong>{{
              valueWithUnit(evaluation.result.water?.pump_head_m, "m")
            }}</strong>
          </div>
        </div>
      </section>
      <template v-if="activeTab === 'water' && evaluation.result"
        ><section v-if="!hydraulic" class="band-section empty-state">
          暂无水力校核数据。冷负荷为 0 或未连接水系统时不会生成水力计算。
        </section>
        <section v-else class="band-section">
          <div class="section-title">
            <h2>水力校核</h2>
            <span>{{ hydraulic.pipes.length }} 个管段</span>
          </div>
          <div class="hydraulic-summary">
            <article class="metric-cell">
              <span>所需泵扬程</span
              ><strong>{{
                valueWithUnit(hydraulic.required_pump_head_m, "m")
              }}</strong
              ><small v-if="hydraulic.most_unfavorable_pipe_id"
                >最不利管段 {{ hydraulic.most_unfavorable_pipe_id }}</small
              >
            </article>
            <article class="metric-cell">
              <span>流量余量</span
              ><strong>{{ margin(hydraulic.pump_check?.flow_margin) }}</strong>
            </article>
            <article class="metric-cell">
              <span>扬程余量</span
              ><strong>{{ margin(hydraulic.pump_check?.head_margin) }}</strong>
            </article>
            <article class="metric-cell">
              <span>泵校核</span
              ><StatusBadge
                :tone="
                  hydraulic.pump_check
                    ? hydraulic.pump_check.status === 'ok'
                      ? 'ok'
                      : hydraulic.pump_check.status === 'error'
                        ? 'error'
                        : 'warn'
                    : 'muted'
                "
                :label="
                  hydraulic.pump_check
                    ? hydraulic.pump_check.status === 'ok'
                      ? '合格'
                      : '告警'
                    : '未校核'
                "
              />
            </article>
          </div>
          <div class="hydraulic-toolbar">
            <div class="flow-chips">
              <span
                v-for="(flow, system) in hydraulic.total_flow_by_system_m3_h"
                :key="system"
                class="flow-chip"
                >{{ SYSTEM_LABELS[system] ?? system }}
                {{ finite(flow, 1) }} m³/h</span
              >
            </div>
            <label
              >系统筛选<select v-model="hydraulicSystemFilter">
                <option value="all">
                  全部（{{ hydraulic.pipes.length }} 段）
                </option>
                <option
                  v-for="system in hydraulicSystems"
                  :key="system"
                  :value="system"
                >
                  {{ SYSTEM_LABELS[system] ?? system }}（{{
                    hydraulic.pipes.filter((pipe) => pipe.system === system)
                      .length
                  }}
                  段）
                </option>
              </select></label
            >
          </div>
          <div v-if="filteredPipes.length" class="table-wrap pipes-table">
            <table>
              <thead>
                <tr>
                  <th>序号</th>
                  <th>管段</th>
                  <th>系统</th>
                  <th>规格</th>
                  <th>流量</th>
                  <th>流速</th>
                  <th>压降</th>
                  <th>沿程扬程</th>
                </tr>
              </thead>
              <tbody>
                <tr
                  v-for="(pipe, index) in filteredPipes"
                  :key="pipe.pipe_id"
                  :class="{
                    'pipe-unfavorable':
                      pipe.pipe_id === hydraulic.most_unfavorable_pipe_id,
                  }"
                >
                  <td>{{ index + 1 }}</td>
                  <td>
                    {{ pipe.pipe_id
                    }}<StatusBadge
                      v-if="pipe.pipe_id === hydraulic.most_unfavorable_pipe_id"
                      tone="warn"
                      label="最不利"
                    />
                  </td>
                  <td>{{ SYSTEM_LABELS[pipe.system] ?? pipe.system }}</td>
                  <td>{{ sizeLabel(pipe.size) }}</td>
                  <td>{{ finite(pipe.flow_m3_h, 2) }} m³/h</td>
                  <td>{{ finite(pipe.velocity_m_s, 2) }} m/s</td>
                  <td>{{ finite(pipe.pressure_drop_pa, 0) }} Pa</td>
                  <td>{{ valueWithUnit(pipe.pump_head_m, "m", 2) }}</td>
                </tr>
              </tbody>
            </table>
          </div>
          <div v-else class="empty-state">当前系统没有管段计算结果。</div>
          <div v-if="hydraulic.paths?.length" class="table-wrap paths-table">
            <h3>环路路径</h3>
            <table>
              <thead>
                <tr>
                  <th>路径</th>
                  <th>系统</th>
                  <th>闭合环路</th>
                  <th>管段数</th>
                  <th>压降</th>
                  <th>扬程</th>
                </tr>
              </thead>
              <tbody>
                <tr
                  v-for="path in hydraulic.paths"
                  :key="path.path_id"
                  :class="{
                    'path-unfavorable':
                      path.path_id === hydraulic.most_unfavorable_path_id,
                  }"
                >
                  <td>
                    {{ path.path_id
                    }}<StatusBadge
                      v-if="path.path_id === hydraulic.most_unfavorable_path_id"
                      tone="warn"
                      label="最不利"
                    />
                  </td>
                  <td>{{ SYSTEM_LABELS[path.system] ?? path.system }}</td>
                  <td>{{ path.closed_circuit ? "是" : "否" }}</td>
                  <td>{{ path.pipe_ids.length }}</td>
                  <td>{{ finite(path.pressure_drop_pa, 0) }} Pa</td>
                  <td>{{ valueWithUnit(path.head_m, "m", 2) }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </section></template
      >
      <section
        v-if="activeTab === 'water' && hydraulic?.pump_check"
        class="band-section"
      >
        <div class="section-title">
          <h2>水泵选配对照</h2>
          <span
            >{{ hydraulic.pump_check.duty_pump_count }} 用
            {{ hydraulic.pump_check.standby_pump_count }} 备</span
          >
        </div>
        <div class="kv-grid">
          <div>
            <span>需求流量</span
            ><strong>{{
              valueWithUnit(hydraulic.pump_check.required_flow_m3_h, "m³/h")
            }}</strong>
          </div>
          <div>
            <span>装机流量</span
            ><strong>{{
              valueWithUnit(hydraulic.pump_check.installed_flow_m3_h, "m³/h")
            }}</strong>
          </div>
          <div>
            <span>需求扬程</span
            ><strong>{{
              valueWithUnit(hydraulic.pump_check.required_head_m, "m")
            }}</strong>
          </div>
          <div>
            <span>可用扬程</span
            ><strong>{{
              valueWithUnit(hydraulic.pump_check.available_head_m, "m")
            }}</strong>
          </div>
        </div>
        <p class="table-note">
          余量为可用值与需求值之比。闭合环路包括供程与回程；水力问题已汇总到“评估结论”。
        </p>
      </section>
      <template v-if="activeTab === 'air' && evaluation.result"
        ><section v-if="!air" class="band-section empty-state">
          暂无风管网络结果，请检查风管与空气系统连接。
        </section>
        <template v-else
          ><section class="band-section">
            <div class="section-title">
              <h2>风系统校核</h2>
              <StatusBadge
                :tone="
                  air.fan_check
                    ? air.fan_check.status === 'ok'
                      ? 'ok'
                      : air.fan_check.status === 'error'
                        ? 'error'
                        : 'warn'
                    : 'muted'
                "
                :label="
                  air.fan_check
                    ? air.fan_check.status === 'ok'
                      ? '风机校核合格'
                      : '需关注'
                    : '未校核'
                "
              />
            </div>
            <div class="kv-grid">
              <div>
                <span>总风量</span
                ><strong>{{
                  valueWithUnit(air.total_airflow_m3_h, "m³/h", 0)
                }}</strong>
              </div>
              <div>
                <span>所需风机静压</span
                ><strong>{{
                  valueWithUnit(air.required_fan_pressure_pa, "Pa", 0)
                }}</strong>
              </div>
              <div>
                <span>风量余量</span
                ><strong>{{ margin(air.fan_check?.airflow_margin) }}</strong>
              </div>
              <div>
                <span>静压余量</span
                ><strong>{{ margin(air.fan_check?.pressure_margin) }}</strong>
              </div>
            </div>
            <div
              v-if="air.solver.degraded || air.solver.fallback_used"
              class="notice-banner"
            >
              当前风网络采用降级计算，请结合问题列表检查连通和风量分配。
            </div>
            <p class="table-note">
              {{
                air.solver.method === "directed_tree_aggregation"
                  ? "按送回风树汇总流量"
                  : "采用有界回退计算"
              }}
              · 最大流量残差
              {{ finite(air.solver.max_flow_residual_m3_h, 3) }} m³/h
            </p>
          </section>
          <section class="band-section">
            <div class="section-title"><h2>风管明细</h2></div>
            <div v-if="air.ducts.length" class="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>风管</th>
                    <th>方向</th>
                    <th>规格</th>
                    <th>风量</th>
                    <th>风速</th>
                    <th>压降</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="duct in air.ducts" :key="duct.duct_id">
                    <td>{{ duct.duct_id }}</td>
                    <td>
                      {{
                        duct.network_side === "supply"
                          ? "送风"
                          : duct.network_side === "return"
                            ? "回风"
                            : "待识别"
                      }}
                    </td>
                    <td>{{ sizeLabel(duct.size) }}</td>
                    <td>{{ finite(duct.airflow_m3_h, 0) }} m³/h</td>
                    <td>{{ finite(duct.velocity_m_s, 2) }} m/s</td>
                    <td>{{ finite(duct.pressure_drop_pa, 0) }} Pa</td>
                  </tr>
                </tbody>
              </table>
            </div>
            <div v-else class="empty-state">当前项目没有参与计算的风管。</div>
          </section></template
        ></template
      >
      <section v-if="activeTab === 'air' && air" class="band-section">
        <div class="section-title">
          <h2>路径与平衡</h2>
          <span>不平衡比 {{ finite(air.balancing.imbalance_ratio, 2) }}</span>
        </div>
        <div v-if="air.paths.length" class="table-wrap air-paths-table">
          <table>
            <thead>
              <tr>
                <th>路径</th>
                <th>完整性</th>
                <th>管段数</th>
                <th>压降</th>
                <th>需平衡压差</th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="path in air.paths"
                :key="path.path_id"
                :class="{
                  'path-unfavorable':
                    path.path_id === air.most_unfavorable_path_id,
                }"
              >
                <td>
                  {{ path.path_id
                  }}<StatusBadge
                    v-if="path.path_id === air.most_unfavorable_path_id"
                    tone="warn"
                    label="最不利"
                  />
                </td>
                <td>{{ path.complete ? "完整" : "断开" }}</td>
                <td>{{ path.pipe_ids.length }}</td>
                <td>{{ valueWithUnit(path.pressure_drop_pa, "Pa", 0) }}</td>
                <td>
                  {{
                    valueWithUnit(
                      air.balancing.path_balancing_pressure_pa[path.path_id] ??
                        path.balancing_pressure_pa,
                      "Pa",
                      0,
                    )
                  }}
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <div v-else class="empty-state">未生成可用的风系统路径。</div>
        <div v-if="air.fan_check" class="fan-parameters kv-grid">
          <div>
            <span>风机装机风量</span
            ><strong>{{
              valueWithUnit(air.fan_check.installed_airflow_m3_h, "m³/h", 0)
            }}</strong>
          </div>
          <div>
            <span>风机可用静压</span
            ><strong>{{
              valueWithUnit(air.fan_check.available_pressure_pa, "Pa", 0)
            }}</strong>
          </div>
        </div>
      </section>
      <template v-if="activeTab === 'energy'"
        ><section v-if="evaluation.result?.simulation" class="band-section">
          <div class="section-title"><h2>稳态仿真</h2></div>
          <div class="kv-grid">
            <div>
              <span>供水温度</span
              ><strong>{{
                valueWithUnit(
                  evaluation.result.simulation.supply_water_temp_c,
                  "°C",
                )
              }}</strong>
            </div>
            <div>
              <span>回水温度</span
              ><strong>{{
                valueWithUnit(
                  evaluation.result.simulation.return_water_temp_c,
                  "°C",
                )
              }}</strong>
            </div>
            <div>
              <span>送风温度</span
              ><strong>{{
                valueWithUnit(
                  evaluation.result.simulation.supply_air_temp_c,
                  "°C",
                )
              }}</strong>
            </div>
            <div>
              <span>估算房间温度</span
              ><strong>{{
                valueWithUnit(
                  evaluation.result.simulation.estimated_room_temp_c,
                  "°C",
                )
              }}</strong>
            </div>
            <div>
              <span>冷机耗功</span
              ><strong>{{
                valueWithUnit(
                  evaluation.result.simulation.cooling_energy_kw,
                  "kW",
                )
              }}</strong>
            </div>
            <div>
              <span>泵功耗</span
              ><strong>{{
                valueWithUnit(evaluation.result.simulation.pump_energy_kw, "kW")
              }}</strong>
            </div>
            <div>
              <span>风机功耗</span
              ><strong>{{
                valueWithUnit(evaluation.result.simulation.fan_energy_kw, "kW")
              }}</strong>
            </div>
            <div>
              <span>总功率</span
              ><strong>{{
                valueWithUnit(
                  evaluation.result.simulation.total_energy_kw,
                  "kW",
                )
              }}</strong>
            </div>
          </div>
        </section>
        <section v-else class="band-section empty-state">
          暂无稳态仿真结果。运行完整评估后查看温度与设备功率。
        </section>
        <section class="band-section">
          <div class="section-title"><h2>能耗仿真</h2></div>
          <div class="energy-form">
            <label
              >仿真小时<input
                v-model.number="energyHours"
                type="number"
                min="1"
                max="8760" /></label
            ><label
              >电价<input
                v-model.number="energyPrice"
                type="number"
                min="0"
                step="0.01" /></label
            ><button
              class="button primary"
              :disabled="!project.project || !energyValid || energyLoading"
              @click="energy"
            >
              <Play />{{ energyLoading ? "计算中…" : "计算能耗" }}
            </button>
          </div>
          <div v-if="evaluation.energy" class="kv-grid">
            <div>
              <span>总能耗</span
              ><strong
                >{{ finite(evaluation.energy.total_energy_kwh) }} kWh</strong
              >
            </div>
            <div>
              <span>预计费用</span
              ><strong>¥ {{ finite(evaluation.energy.estimated_cost) }}</strong>
            </div>
          </div>
          <div v-else class="empty-state">设置时长与电价后计算能耗。</div>
        </section></template
      >
      <section
        v-if="!evaluation.result && activeTab !== 'energy'"
        class="band-section empty-state"
      >
        <h2>从一次完整评估开始</h2>
        <p>
          {{
            project.project
              ? "运行评估后查看各系统结果。"
              : "请先打开项目或选择教学样例。"
          }}
        </p>
        <button
          class="button primary"
          :disabled="!project.project"
          @click="run"
        >
          运行评估
        </button>
      </section>
    </div>
  </div>
</template>

<style scoped>
/* 房间负荷明细：楼层（第一级）→ 房间（第二级）两级树，点楼层行开合。 */
.load-floor-row > td {
  background: #f2f7f6;
  font-weight: 600;
}
.load-tree-cell {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}
.load-tree-expand {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 18px;
  height: 18px;
  padding: 0;
  border: 0;
  border-radius: 4px;
  background: transparent;
  color: #4d6b7a;
  cursor: pointer;
}
.load-tree-expand:hover {
  background: #e2edeb;
}
.load-tree-chevron {
  width: 14px;
  height: 14px;
  transition: transform 0.15s ease;
}
.load-tree-expand[aria-expanded="true"] .load-tree-chevron {
  transform: rotate(90deg);
}
.load-tree-meta {
  color: #7d8da2;
  font-size: 11px;
  font-weight: 400;
}
/* 第二级缩进，让从属关系一眼可见 */
.load-room-row .load-room-name {
  padding-left: 28px;
}
.load-room-row .load-room-name::before {
  content: "↳ ";
  color: #9fb0c0;
}
.evaluation-workspace .metric-grid {
  grid-template-columns: repeat(4, minmax(0, 1fr));
}
.review-conclusion {
  border-left: 3px solid #2f6fed;
}
.review-conclusion h2 {
  margin: 5px 0 8px;
}
.review-conclusion p:last-child {
  color: #667991;
  line-height: 1.7;
}
.review-issues article {
  display: flex;
  gap: 14px;
  padding: 15px 0;
  border-bottom: 1px solid #e8edf5;
}
.review-issues small {
  display: block;
  color: #7d8da2;
  margin-top: 5px;
}
.recommendation-list {
  margin-top: 18px;
  padding: 16px;
  background: #f3f7fd;
  border-radius: 10px;
}
.review-two-columns {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 20px;
}
.hydraulic-summary {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 12px;
  margin-bottom: 18px;
}
.hydraulic-toolbar {
  display: flex;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 12px;
  margin: 18px 0;
}
.flow-chips {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}
.flow-chip {
  padding: 7px 10px;
  border-radius: 6px;
  background: #edf3ff;
  color: #27529a;
  font-size: 12px;
}
.pipe-unfavorable,
.path-unfavorable {
  background: #fff9ed;
}
.paths-table {
  margin-top: 24px;
}
.paths-table h3 {
  font-size: 14px;
  margin: 0 0 14px;
}
.review-paths article {
  display: flex;
  gap: 14px;
  align-items: flex-start;
  padding: 15px 0;
  border-bottom: 1px solid #e8edf5;
}
.review-paths article:last-child {
  border-bottom: 0;
}
.review-paths p {
  color: #667991;
  line-height: 1.7;
  overflow-wrap: anywhere;
  margin: 8px 0 0;
}
.fan-parameters {
  margin-top: 24px;
}
.hydraulic-toolbar > label,
.section-title > label {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  color: #667991;
}
td .status-badge {
  margin-left: 8px;
}
.kv-grid {
  row-gap: 24px;
}
.energy-form {
  display: flex;
  align-items: end;
  gap: 16px;
  flex-wrap: wrap;
}
.energy-form label {
  display: grid;
  gap: 7px;
  color: #667991;
  font-size: 13px;
}
@media (max-width: 1100px) {
  .evaluation-workspace .metric-grid,
  .hydraulic-summary {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}
@media (max-width: 720px) {
  .review-two-columns {
    grid-template-columns: 1fr;
  }
}
</style>
