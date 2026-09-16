<script setup lang="ts">
import { computed, onBeforeUnmount, watch } from "vue";
import { Calculator, Plus, RotateCcw, Trash2, TriangleAlert } from "lucide-vue-next";
import { useLoadStore } from "@/stores/load";
import { useProjectStore } from "@/stores/project";
import { ROOM_PURPOSE_OPTIONS } from "@/utils/project-editor";
import {
  ADVANCED_FIELDS,
  COOLING_COMPONENT_KEYS,
  COMPONENT_LABELS,
  DESIGN_FIELDS,
  ORIENTATION_OPTIONS,
  ROOM_FIELDS,
  componentKeys,
  sumComponents,
  type FieldSpec,
} from "@/utils/load";
import { finite } from "@/utils/format";
import type { Issue, RoomInput } from "@/types";

const project = useProjectStore();
const store = useLoadStore();

// 以当前项目的房间为底稿。同一项目重复挂载不会覆盖已改的草稿（store 内部按
// seedKey 去重），换项目才重新播种并算一次。
watch(
  () => project.selectionKey,
  (key) => {
    store.seedFromProject(project.project, key ?? "no-project");
    void store.recalculate();
  },
  { immediate: true },
);
onBeforeUnmount(() => store.cancelScheduled());

const result = computed(() => store.result);
const componentTotals = computed(() => (result.value ? sumComponents(result.value.rooms) : {}));
const visibleComponents = computed(() =>
  result.value ? componentKeys(result.value.rooms) : [],
);
const coolingTotal = computed(() => result.value?.total_cooling_kw ?? 0);
/** 冷负荷分项之和。后端保证它恒等于总冷负荷，这里把它显示出来供核对。 */
const coolingComponentSum = computed(() =>
  COOLING_COMPONENT_KEYS.reduce((total, key) => total + (componentTotals.value[key] ?? 0), 0),
);
const conservationGap = computed(() => coolingComponentSum.value - coolingTotal.value);
const totalArea = computed(() => store.rooms.reduce((sum, room) => sum + numberField(room, "area_m2"), 0));
const totalPeople = computed(() => store.rooms.reduce((sum, room) => sum + numberField(room, "people"), 0));
const densityWm2 = computed(() => (totalArea.value > 0 ? (coolingTotal.value * 1000) / totalArea.value : 0));
/** 各房间取值不一致的设计参数——界面不能假装全楼统一。 */
const inconsistent = computed(() =>
  DESIGN_FIELDS.filter((field) => store.sharedValue(field.key) === null).map((field) => field.label),
);
const projectName = computed(() => project.active?.name?.replace("（未保存）", "") || "未选择项目");

function numberField(room: RoomInput, key: string): number {
  const value = room[key];
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}
function textField(room: RoomInput, key: string): string {
  const value = room[key];
  return typeof value === "string" ? value : "";
}
function stringField(room: RoomInput, key: string): string {
  const value = room[key];
  return typeof value === "string" ? value : "";
}
function designValue(key: string): number | string {
  return store.sharedValue(key) ?? "";
}

function parsedNumber(event: Event): number | null {
  const raw = (event.target as HTMLInputElement).value;
  if (raw.trim() === "") return null;
  const parsed = Number(raw);
  return Number.isFinite(parsed) ? parsed : null;
}

function onRoomNumber(index: number, key: string, event: Event) {
  const parsed = parsedNumber(event);
  if (parsed === null) return;
  // 后端 `people` 是整数，30.5 会被判 422；这里就地取整。
  store.updateRoom(index, key, key === "people" ? Math.max(0, Math.round(parsed)) : parsed);
}
function onRoomText(index: number, key: string, event: Event) {
  store.updateRoom(index, key, (event.target as HTMLInputElement).value);
}
function onRoomChoice(index: number, key: string, event: Event) {
  store.updateRoom(index, key, (event.target as HTMLSelectElement).value);
}
function onDesign(field: FieldSpec, event: Event) {
  const parsed = parsedNumber(event);
  if (parsed === null) return;
  store.applyToAllRooms(field.key, parsed);
}
function onFactor(key: "simultaneityFactor" | "safetyFactor", event: Event) {
  const parsed = parsedNumber(event);
  if (parsed === null) return;
  store.setFactor(key, parsed);
}

function componentShare(key: string): number {
  return coolingTotal.value > 0 ? ((componentTotals.value[key] ?? 0) / coolingTotal.value) * 100 : 0;
}
function barWidth(key: string): string {
  const values = visibleComponents.value.map((item) => Math.abs(componentTotals.value[item] ?? 0));
  const max = Math.max(...values, 0);
  if (max <= 0) return "0%";
  return `${Math.min(100, (Math.abs(componentTotals.value[key] ?? 0) / max) * 100)}%`;
}
function issueTone(level: Issue["level"]) {
  return level === "error" ? "error" : level === "warning" ? "warn" : "muted";
}
function floorLabel(id: unknown): string {
  const found = store.floors.find((floor) => floor.id === id);
  return found?.name || (typeof id === "string" && id) || "未指定";
}
/** 结果里没有面积字段，而 `load.py` 按 `request.rooms` 的顺序产出，
 *  因此草稿与结果**按位置一一对应**，用下标取面积。 */
function draftArea(index: number): number {
  return numberField(store.rooms[index] ?? {}, "area_m2");
}
</script>

<template>
  <div class="view-stack load-view">
    <section class="view-heading">
      <div>
        <p class="eyebrow">LOAD CALCULATION / 分项法</p>
        <h1>负荷计算</h1>
        <p>
          以<strong>{{ projectName }}</strong>的房间为底稿，改参数即时重算。
          这里只算不改：草稿留在本页，不会写回项目，也不影响已保存的数据。
        </p>
      </div>
      <div class="toolbar">
        <button class="button secondary" type="button" :disabled="!store.baseline" @click="store.restoreBaseline()">
          <RotateCcw />恢复项目原值
        </button>
        <button class="button primary" type="button" @click="store.recalculate()">
          <Calculator />重新计算
        </button>
      </div>
    </section>

    <div v-if="store.error" class="error-banner" role="alert">{{ store.error }}</div>

    <div v-if="!store.rooms.length" class="empty-state large">
      <div>
        <strong>还没有可算的房间</strong>
        <span>这个页面以当前项目的房间为底稿。请在顶部选一个项目或样例；也可以直接手动加一间房间试试。</span>
        <button class="button secondary" type="button" @click="store.addRoom()"><Plus />新增房间</button>
      </div>
    </div>

    <template v-else>
      <section class="band-section">
        <div class="section-title">
          <div><p class="eyebrow">DESIGN CONDITIONS</p><h2>全楼设计参数</h2></div>
          <span>一次改到所有房间</span>
        </div>
        <p class="note">
          后端把设计参数存在每个房间上，这里替你统一设置。温度与湿度是冷热负荷的基准，
          改变它们会同时影响新风、围护与潜热三项。
        </p>
        <div class="field-grid">
          <label v-for="field in DESIGN_FIELDS" :key="field.key">
            <span>{{ field.label }}<em v-if="field.unit">（{{ field.unit }}）</em></span>
            <input
              type="number" :step="field.step" :min="field.min" :max="field.max"
              :data-field="field.key"
              :value="designValue(field.key)" :title="field.hint" @change="onDesign(field, $event)"
            />
            <small>{{ field.hint }}</small>
          </label>
          <label>
            <span>同时使用系数</span>
            <input type="number" step="0.05" min="0.1" max="1" :value="store.simultaneityFactor"
                   title="调整后负荷 = 总负荷 × 同时使用系数 × 安全系数" @change="onFactor('simultaneityFactor', $event)" />
            <small>修正总负荷用，不改变分项</small>
          </label>
          <label>
            <span>安全系数</span>
            <input type="number" step="0.05" min="1" :value="store.safetyFactor"
                   title="同上" @change="onFactor('safetyFactor', $event)" />
            <small>选型裕量，一般 1.05~1.15</small>
          </label>
        </div>
        <div v-if="inconsistent.length" class="warning-banner">
          <TriangleAlert />这些参数在各房间取值不一致：{{ inconsistent.join('、') }}。改动会把它们统一。
        </div>
      </section>

      <section class="band-section">
        <div class="section-title">
          <div><p class="eyebrow">ROOMS</p><h2>房间参数</h2></div>
          <button class="button secondary" type="button" @click="store.addRoom()"><Plus />新增房间</button>
        </div>
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>房间</th><th>楼层</th>
                <th v-for="field in ROOM_FIELDS" :key="field.key">{{ field.label }}<em v-if="field.unit">（{{ field.unit }}）</em></th>
                <th>用途</th><th>朝向</th><th></th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="(room, index) in store.rooms" :key="`${room.id ?? 'new'}-${index}`">
                <td><input data-field="name" :value="textField(room, 'name')" @change="onRoomText(index, 'name', $event)" /></td>
                <td>
                  <select data-field="floor_id" :value="room.floor_id ?? ''" @change="onRoomChoice(index, 'floor_id', $event)">
                    <option value="">未指定</option>
                    <option v-for="floor in store.floors" :key="floor.id" :value="floor.id">{{ floor.name || floor.id }}</option>
                  </select>
                </td>
                <td v-for="field in ROOM_FIELDS" :key="field.key">
                  <input
                    type="number" :step="field.step" :min="field.min"
                    :data-field="field.key"
                    :value="numberField(room, field.key)" :title="field.hint"
                    @change="onRoomNumber(index, field.key, $event)"
                  />
                </td>
                <td>
                  <select data-field="purpose" :value="stringField(room, 'purpose')" @change="onRoomChoice(index, 'purpose', $event)">
                    <option v-for="option in ROOM_PURPOSE_OPTIONS" :key="option.value" :value="option.value">
                      {{ option.label }}
                    </option>
                  </select>
                </td>
                <td>
                  <select data-field="orientation" :value="stringField(room, 'orientation') || 'south'" @change="onRoomChoice(index, 'orientation', $event)">
                    <option v-for="option in ORIENTATION_OPTIONS" :key="option.value" :value="option.value">{{ option.label }}</option>
                  </select>
                </td>
                <td><button class="button ghost" type="button" title="删除该房间" @click="store.removeRoom(index)"><Trash2 /></button></td>
              </tr>
            </tbody>
          </table>
        </div>
        <p class="note">
          <strong>用途决定负荷指标</strong>：冷热指标、照明、设备与每人新风量都按用途取值
          （后端 `PURPOSE_DEFAULTS`），所以想改指标要改用用途或改下面的高级参数。
        </p>

        <details class="advanced">
          <summary>高级参数（逐房间：层高 / 窗墙比 / 围护传热 / 外区系数）</summary>
          <div class="table-wrap">
            <table>
              <thead>
                <tr><th>房间</th><th v-for="field in ADVANCED_FIELDS" :key="field.key">{{ field.label }}<em v-if="field.unit">（{{ field.unit }}）</em></th></tr>
              </thead>
              <tbody>
                <tr v-for="(room, index) in store.rooms" :key="`adv-${index}`">
                  <td>{{ textField(room, 'name') || '未命名' }}</td>
                  <td v-for="field in ADVANCED_FIELDS" :key="field.key">
                    <input
                      type="number" :step="field.step" :min="field.min" :max="field.max"
                      :value="numberField(room, field.key)" :title="field.hint"
                      @change="onRoomNumber(index, field.key, $event)"
                    />
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </details>
      </section>

      <section v-if="result" class="band-section">
        <div class="section-title">
          <div><p class="eyebrow">RESULT</p><h2>计算结果</h2></div>
          <span v-if="store.state === 'loading'">重算中…</span>
        </div>

        <div class="metric-grid">
          <div class="metric-cell"><span>总冷负荷（分项法）</span><strong>{{ finite(result.total_cooling_kw, 2) }} kW</strong><small>{{ store.rooms.length }} 个房间 · {{ finite(totalArea, 1) }} m² · {{ totalPeople }} 人</small></div>
          <div class="metric-cell"><span>调整后冷负荷</span><strong>{{ finite(result.adjusted_cooling_kw, 2) }} kW</strong><small>总冷负荷 × {{ finite(store.simultaneityFactor, 2) }} × {{ finite(store.safetyFactor, 2) }}，选型用这个</small></div>
          <div class="metric-cell"><span>单位面积冷负荷</span><strong>{{ finite(densityWm2, 1) }} W/m²</strong><small>分项法实算，不是用途指标</small></div>
          <div class="metric-cell"><span>总热负荷</span><strong>{{ finite(result.total_heating_kw, 2) }} kW</strong><small>调整后 {{ finite(result.adjusted_heating_kw, 2) }} kW</small></div>
        </div>

        <div class="result-body">
          <div class="components">
            <h3>冷负荷分项构成</h3>
            <ul class="component-list">
              <li v-for="key in visibleComponents" :key="key">
                <span class="component-name">{{ COMPONENT_LABELS[key] ?? key }}</span>
                <span class="component-bar"><i :style="{ width: barWidth(key) }"></i></span>
                <span class="component-value">{{ finite(componentTotals[key], 2) }} kW</span>
                <span class="component-share">
                  {{ COOLING_COMPONENT_KEYS.includes(key) ? `${finite(componentShare(key), 1)}%` : '不计入冷负荷' }}
                </span>
              </li>
            </ul>
            <p class="note" :class="{ bad: Math.abs(conservationGap) > 0.02 }">
              冷负荷分项合计 {{ finite(coolingComponentSum, 2) }} kW
              {{ Math.abs(conservationGap) <= 0.02 ? '＝ 总冷负荷（守恒）' : `≠ 总冷负荷（差 ${finite(conservationGap, 2)} kW）` }}。
              供暖两项单列，不计入冷负荷。
            </p>
          </div>

          <div class="per-room">
            <h3>逐房间明细<span v-if="store.state === 'loading'" class="pending">重算中，下表为上一次结果</span></h3>
            <div class="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>房间</th><th>楼层</th><th>面积 (m²)</th>
                    <th>冷负荷 (kW)</th><th>热负荷 (kW)</th><th>新风 (m³/h)</th>
                    <th>指标法校核 (kW)</th><th>偏差</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="(room, index) in result.rooms" :key="`${index}-${room.name}`">
                    <td>{{ room.name }}</td>
                    <td>{{ floorLabel(room.floor_id) }}</td>
                    <td>{{ finite(draftArea(index), 1) }}</td>
                    <td><strong>{{ finite(room.cooling_kw, 2) }}</strong></td>
                    <td>{{ finite(room.heating_kw, 2) }}</td>
                    <td>{{ finite(room.fresh_air_m3_h, 0) }}</td>
                    <td>{{ finite(room.index_estimate_kw, 2) }}</td>
                    <td :class="{ bad: Math.abs(room.index_deviation_pct) > 30 }">{{ finite(room.index_deviation_pct, 1) }}%</td>
                  </tr>
                </tbody>
              </table>
            </div>
            <p class="note">
              「指标法校核」是按用途的面积指标算出来的参考值，**不计入**分项冷负荷；两列相差越大，
              说明这间房的负荷结构越偏离该用途的常规水平。
            </p>
          </div>
        </div>

        <div v-if="result.floor_loads.length" class="floor-table">
          <h3>分楼层汇总</h3>
          <div class="table-wrap">
            <table>
              <thead>
                <tr><th>楼层</th><th>房间数</th><th>面积 (m²)</th><th>人数</th><th>冷负荷 (kW)</th><th>调整后 (kW)</th><th>热负荷 (kW)</th></tr>
              </thead>
              <tbody>
                <tr v-for="floor in result.floor_loads" :key="floor.floor_id">
                  <td>{{ floor.floor_name || floorLabel(floor.floor_id) }}</td>
                  <td>{{ floor.room_count }}</td>
                  <td>{{ finite(floor.area_m2, 1) }}</td>
                  <td>{{ floor.people }}</td>
                  <td><strong>{{ finite(floor.cooling_kw, 2) }}</strong></td>
                  <td>{{ finite(floor.adjusted_cooling_kw, 2) }}</td>
                  <td>{{ finite(floor.heating_kw, 2) }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>

        <div v-if="result.issues.length" class="issues">
          <h3>计算提示</h3>
          <ul>
            <li v-for="issue in result.issues" :key="`${issue.code}-${issue.target ?? ''}`">
              <span class="issue-badge" :class="issueTone(issue.level)">{{ issue.level === 'error' ? '错误' : issue.level === 'warning' ? '告警' : '提示' }}</span>
              <span>{{ issue.message }}</span>
              <code v-if="issue.code === 'HIGH_COOLING_LOAD_DENSITY'">{{ issue.target }}</code>
            </li>
          </ul>
        </div>
        <p v-else class="note">本次计算没有产生任何提示——负荷密度、指标偏差都在常规范围内。</p>
      </section>

      <div v-else class="empty-state large">
        <div>
          <strong>{{ store.state === 'loading' ? '正在计算…' : '还没有结果' }}</strong>
          <span>改任意参数会自动重算；也可以点右上角「重新计算」。</span>
        </div>
      </div>
    </template>
  </div>
</template>

<style scoped>
.load-view { background: var(--surface-soft); padding-bottom: 24px; }
.load-view h1 { color: var(--teal-dark); font-size: clamp(24px, 2.6vw, 34px); letter-spacing: -.02em; }
.load-view .view-heading { align-items: flex-start; flex-wrap: wrap; gap: 16px; }
.load-view .view-heading p:not(.eyebrow) { max-width: 780px; line-height: 1.8; }
.note { margin-top: 12px; color: var(--muted); font-size: 12px; line-height: 1.8; }
.note.bad, .bad { color: #b3372c; }
.field-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: 16px; }
.field-grid small { color: #869296; font-size: 11px; font-weight: 400; line-height: 1.6; }
.field-grid em, table em { font-style: normal; font-weight: 400; color: var(--muted); }
.warning-banner { display: flex; align-items: center; gap: 8px; margin-top: 14px; }
.warning-banner > svg { width: 15px; flex: none; color: #b17a1d; }
.load-view table input, .load-view table select { width: 100%; min-width: 74px; height: 34px; }
.advanced { margin-top: 18px; }
.advanced summary { cursor: pointer; color: var(--teal); font-size: 12px; font-weight: 650; padding: 8px 0; }
.advanced .table-wrap { margin-top: 10px; }
.result-body { display: grid; grid-template-columns: minmax(0, 340px) minmax(0, 1fr); gap: 22px; margin-top: 22px; }
.result-body h3, .floor-table h3, .issues h3 { margin-bottom: 12px; color: var(--teal-dark); font-size: 14px; }
.pending { margin-left: 9px; color: #b17a1d; font-size: 11px; font-weight: 400; }
.component-list { display: grid; gap: 8px; }
.component-list li { display: grid; grid-template-columns: 76px minmax(0, 1fr) 72px 78px; align-items: center; gap: 8px; font-size: 12px; }
.component-name { color: #526165; }
.component-bar { height: 9px; background: #e8eef4; border-radius: 5px; overflow: hidden; }
.component-bar i { display: block; height: 100%; background: linear-gradient(90deg, #5b8fc4, #7db4e0); border-radius: 5px; }
.component-value { text-align: right; font-variant-numeric: tabular-nums; color: var(--teal-dark); font-weight: 600; }
.component-share { text-align: right; color: var(--muted); font-variant-numeric: tabular-nums; }
.floor-table { margin-top: 22px; }
.issues { margin-top: 22px; }
.issues ul { display: grid; gap: 8px; }
.issues li { display: flex; align-items: baseline; gap: 9px; font-size: 12px; line-height: 1.7; color: #40525e; }
.issue-badge { flex: none; padding: 2px 7px; border-radius: 4px; font-size: 10px; font-weight: 650; color: #fff; background: #8a99a5; }
.issue-badge.error { background: #c0392b; }
.issue-badge.warn { background: #c98a1c; }
.issues code { color: var(--muted); font-size: 11px; }
.empty-state.large > div { display: grid; gap: 10px; justify-items: center; text-align: center; }
.empty-state.large span { max-width: 560px; line-height: 1.8; }
@media (max-width: 1100px) { .result-body { grid-template-columns: 1fr; } }
</style>
