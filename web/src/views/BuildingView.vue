<script setup lang="ts">
import { computed, ref, watch, type Ref } from "vue";
import {
  Building2,
  Cable,
  ChevronRight,
  Layers,
  Network,
  Plus,
  RotateCcw,
  Save,
  Trash2,
} from "lucide-vue-next";
import { api } from "@/api/client";
import { useProjectStore } from "@/stores/project";
import { useRealtimeStore } from "@/stores/realtime";
import { useUiStore } from "@/stores/ui";
import { finite } from "@/utils/format";
import {
  DEVICE_LOCATION_OPTIONS,
  DEVICE_TYPE_OPTIONS,
  HVAC_SYSTEMS,
  PIPE_TYPES,
  PIPE_TYPE_LABELS,
  ROOM_PURPOSE_OPTIONS,
  SYSTEM_LABELS,
  addConnection,
  addDevice,
  addFloor,
  addPipe,
  addRoom,
  buildTree,
  cloneProjectData,
  deviceLocationLabel,
  editorNodeOptions,
  isKnownDeviceLocation,
  removeConnection,
  removeDeviceAt,
  removeFloor,
  removePipeAt,
  removeRoom,
  renameFloor,
  roomIdsOnFloor,
  roomKey,
  updateConnection,
  updateDeviceAt,
  updatePipeAt,
  updateRoom,
  terminalDevices,
  validateProjectDraft,
} from "@/utils/building";
import type {
  CatalogItem,
  ConnectionInput,
  DeviceInput,
  FloorInput,
  PipeInput,
  ProjectData,
  RoomInput,
} from "@/types";

const project = useProjectStore();
const realtime = useRealtimeStore();
const ui = useUiStore();
const activeSection = ref("rooms");
const sections = [{ id: "rooms", label: "房间与末端" }, { id: "devices", label: "设备清单" }, { id: "network", label: "管网与连接" }];

// All edits happen on a local, plain-data draft. The store remains the
// server-confirmed source of truth until the user presses 保存结构.
const draft = ref<ProjectData | null>(null);
const saved = ref(false);
const notice = ref("");

/** 当前选中的楼层（编辑主轴）。切层时右侧详情随之过滤。 */
const activeFloorId = ref<string>("");
/** 展开详情的设备/管路行（跨层保留，切层不丢）。 */
const expandedDeviceIds = ref<Set<string>>(new Set());
const expandedPipeIds = ref<Set<string>>(new Set());
/** 新增设备弹层。 */
const deviceModalOpen = ref(false);
const deviceForm = ref<{ type: string; name: string; floorId: string; quantity: number }>({
  type: "风机盘管",
  name: "",
  floorId: "",
  quantity: 1,
});
/** 设备参数库（型号下拉数据源，加载失败不阻塞编辑）。 */
const catalogItems = ref<CatalogItem[]>([]);
const catalogLoaded = ref(false);

const tree = computed(() => (draft.value ? buildTree(draft.value) : { nodes: [], orphans: [] }));
const devices = computed(() => draft.value?.devices ?? []);
const terminalDeviceList = computed(() => (draft.value ? terminalDevices(draft.value) : []));
const pipes = computed(() => draft.value?.pipes ?? []);
const connections = computed(() => draft.value?.connections ?? []);
const nodeOptions = computed(() => (draft.value ? editorNodeOptions(draft.value) : []));
const editable = computed(() => !project.usingSample && !realtime.running && draft.value !== null);
const dirty = computed(
  () => draft.value !== null && JSON.stringify(draft.value) !== JSON.stringify(project.project),
);
watch(dirty, (value) => { ui.buildingDirty = value; }, { immediate: true });
const roomCount = computed(() => draft.value?.rooms.length ?? 0);
const totalArea = computed(() =>
  (draft.value?.rooms ?? []).reduce(
    (sum, room) => sum + (Number.isFinite(room.area_m2) ? room.area_m2 : 0),
    0,
  ),
);
const areasValid = computed(() =>
  (draft.value?.rooms ?? []).every((room) => Number.isFinite(room.area_m2) && room.area_m2 > 0),
);
const namesValid = computed(() =>
  (draft.value?.rooms ?? []).every((room) => (room.name ?? "").trim().length > 0),
);
const validation = computed(() =>
  draft.value ? validateProjectDraft(draft.value) : { valid: false, issues: [] },
);
const canSave = computed(
  () => editable.value && dirty.value && areasValid.value && namesValid.value && validation.value.valid,
);

/** 选中楼层节点。activeFloorId 无效时回退到第一层；特殊值 "risers" 选中立管视图。 */
const RISER_VIEW = "risers";
const activeNode = computed(() => {
  if (!tree.value.nodes.length) return null;
  return tree.value.nodes.find((node) => node.floor.id === activeFloorId.value) ?? tree.value.nodes[0];
});
const isRiserView = computed(() => activeFloorId.value === RISER_VIEW);

/** 立管：riser 类型且未指定楼层的管段，贯穿多层、不归属任何单层。
 *  单独成组（楼层导航「立管 / 全楼共用」），避免每层视图被跨层枢纽淹没。 */
function isRiserPipe(pipe: PipeInput): boolean {
  return (
    !pipe.floor_id &&
    (pipe.pipe_type === "riser_supply" || pipe.pipe_type === "riser_return")
  );
}

/** 当前层的设备：楼层未指定的设备归入当前层（任何层都能编辑），
 *  楼层指向其他有效层的设备只在那一层出现。 */
const floorDevices = computed(() => {
  if (!draft.value || !activeNode.value) return [];
  const floorId = activeNode.value.floor.id;
  return draft.value.devices
    .map((device, index) => ({ device, index }))
    .filter(({ device }) => !device.floor_id || device.floor_id === floorId);
});

const floorPipes = computed(() => {
  if (!draft.value || !activeNode.value) return [];
  const floorId = activeNode.value.floor.id;
  return draft.value.pipes
    .map((pipe, index) => ({ pipe, index }))
    .filter(({ pipe }) => pipe.floor_id === floorId && !isRiserPipe(pipe));
});

/** 立管视图：全部立管 + 未指定楼层且非立管的管段（无处安放的兜底）。 */
const riserPipes = computed(() => {
  if (!draft.value) return [];
  return draft.value.pipes
    .map((pipe, index) => ({ pipe, index }))
    .filter(({ pipe }) => isRiserPipe(pipe));
});

/** 节点的楼层归属（立管视为不属于任何单层）。 */
function nodeOnFloor(nodeId: string, floorId: string): boolean {
  const device = draft.value?.devices.find((item) => item.id === nodeId);
  if (device) return !device.floor_id || device.floor_id === floorId;
  const pipe = draft.value?.pipes.find((item) => item.id === nodeId);
  if (pipe) {
    if (isRiserPipe(pipe)) return false;
    return !pipe.floor_id || pipe.floor_id === floorId;
  }
  return true;
}

/** 当前层连接：两端都挂在本层才进楼层视图；
 *  立管参与的连接（垂直输配）归立管视图，不"沾"进每一层。 */
const floorConnections = computed(() => {
  if (!draft.value || !activeNode.value) return [];
  const floorId = activeNode.value.floor.id;
  return draft.value.connections
    .map((connection, index) => ({ connection, index }))
    .filter(({ connection }) =>
      nodeOnFloor(connection.from_id, floorId) && nodeOnFloor(connection.to_id, floorId),
    );
});

/** 立管视图的连接：起终点任一为立管（或未归属的节点）。 */
const riserConnections = computed(() => {
  if (!draft.value) return [];
  const involvesRiser = (nodeId: string): boolean => {
    const pipe = draft.value!.pipes.find((item) => item.id === nodeId);
    return pipe ? isRiserPipe(pipe) || !pipe.floor_id : false;
  };
  return draft.value.connections
    .map((connection, index) => ({ connection, index }))
    .filter(({ connection }) =>
      involvesRiser(connection.from_id) || involvesRiser(connection.to_id),
    );
});

/** 参数库型号下拉：按设备类型分组，值为「型号」。Unity 摆放件无工程参数，不进选择器。 */
const catalogModelsByType = computed(() => {
  const grouped = new Map<string, string[]>();
  for (const item of catalogItems.value) {
    const type = (item.设备类型 ?? "").trim();
    const model = (item.型号 ?? "").trim();
    if (!type || !model || model === "*" || item["仅摆放"] === true) continue;
    const list = grouped.get(type) ?? [];
    list.push(model);
    grouped.set(type, list);
  }
  return grouped;
});
const catalogModelOptions = computed(
  () => catalogModelsByType.value.get(deviceForm.value.type) ?? [],
);
/** 选型后自动带出的额定参数预览（只读展示，落盘仍是后端目录解析）。 */
const selectedCatalogItem = computed(() =>
  catalogItems.value.find(
    (item) =>
      (item.设备类型 ?? "").trim() === deviceForm.value.type &&
      (item.型号 ?? "").trim() === deviceForm.value.name,
  ),
);

function resetDraft() {
  // Never pass a Pinia proxy to structuredClone. The explicit copier keeps
  // nested room bindings and unknown backend fields intact.
  draft.value = project.project ? cloneProjectData(project.project) : null;
  saved.value = false;
  notice.value = "";
}

watch(() => project.project, resetDraft, { immediate: true });
watch(
  () => project.selectionKey,
  () => {
    activeFloorId.value = "";
    activeSection.value = "rooms";
    expandedDeviceIds.value = new Set();
    expandedPipeIds.value = new Set();
    resetDraft();
  },
);

async function loadCatalog() {
  try {
    catalogItems.value = await api<CatalogItem[]>("/api/v1/catalog/equipment");
  } catch {
    catalogItems.value = [];
  } finally {
    catalogLoaded.value = true;
  }
}

loadCatalog();

function selectFloor(floorId: string) {
  activeFloorId.value = floorId;
}

function toggleIn(set: { value: Set<string> }, id: string) {
  const next = new Set(set.value);
  if (next.has(id)) next.delete(id);
  else next.add(id);
  set.value = next;
}

function toggleDeviceRow(rowKey: string) {
  toggleIn(expandedDeviceIds, rowKey);
}

function togglePipeRow(rowKey: string) {
  toggleIn(expandedPipeIds, rowKey);
}

function onAddFloor() {
  if (!draft.value) return;
  const result = addFloor(draft.value);
  draft.value = result.project;
  activeFloorId.value = result.floorId;
  notice.value = `已新增楼层 ${result.floorId}，保存后生效。`;
}

function onAddRoom(floorId: string) {
  if (!draft.value) return;
  const result = addRoom(draft.value, floorId);
  draft.value = result.project;
  notice.value = `已新增房间 ${result.roomId}，保存后生效。`;
}

/** 删楼层会连带删掉整层房间。房间自己持有 device_ids，绑定随房间一起消失。 */
function onRemoveFloor(floorId: string, floorLabel: string) {
  if (!draft.value) return;
  const doomedRooms = roomIdsOnFloor(draft.value, floorId);
  const lines = [`确定删除「${floorLabel}」？`];
  if (doomedRooms.length) lines.push(`该层 ${doomedRooms.length} 个房间及其末端绑定会一并删除。`);
  if (!window.confirm(lines.join("\n"))) return;
  draft.value = removeFloor(draft.value, floorId);
  if (activeFloorId.value === floorId) activeFloorId.value = "";
  notice.value = `已删除 ${floorLabel}${doomedRooms.length ? ` 及其 ${doomedRooms.length} 个房间` : ""}。`;
}

function onRemoveRoom(roomId: string, roomLabel: string) {
  if (!draft.value) return;
  const room = draft.value.rooms.find((item, index) => roomKey(item, index) === roomId);
  const bound = room?.device_ids ?? [];
  const lines = [`确定删除房间「${roomLabel}」？`];
  if (bound.length) lines.push(`该房间绑定的末端 ${bound.join("、")} 将不再服务它。`);
  if (!window.confirm(lines.join("\n"))) return;
  draft.value = removeRoom(draft.value, roomId);
  notice.value = `已删除房间 ${roomLabel}。`;
}

function onRenameFloor(floorId: string, value: string) {
  if (draft.value) draft.value = renameFloor(draft.value, floorId, value);
}

function onPatchRoom(roomId: string, patch: Partial<RoomInput>) {
  if (draft.value) draft.value = updateRoom(draft.value, roomId, patch);
}

/** 房间卡片上解绑某个末端。 */
function onUnbindDevice(roomId: string, deviceId: string) {
  const room = draft.value?.rooms.find((item, index) => roomKey(item, index) === roomId);
  if (!room) return;
  const kept = (room.device_ids ?? []).filter((id) => id !== deviceId);
  onPatchRoom(roomId, { device_ids: kept });
}

/**
 * 未选中楼层节点时，新增设备/管路的落位楼层。
 *
 * 与后端 `app/building.py` 的 `implicit_floor_id` 同序（`normalized_floors`：
 * 层级 → 标高 → id），这样界面显示的归属与楼层汇总算出来的归属一致。
 * 以前这里留空：后端会把"未声明楼层"兜底成一个不存在的 `default_floor` 并报
 * `UNKNOWN_*_FLOOR` error，"新增管路"在默认路径上就会变红（已修，见
 * `tests/test_floor_fallback.py`）。现在后端不再报错，显式写出楼层是为了让
 * 新节点被算进用户正在看的那一层，而不是悄悄归到最低层。
 */
function fallbackFloorId(): string {
  const floors = draft.value?.floors ?? [];
  const sorted = [...floors].sort(
    (a, b) =>
      a.level - b.level ||
      a.elevation_m - b.elevation_m ||
      String(a.id).localeCompare(String(b.id)),
  );
  return sorted[0]?.id ?? "";
}

function openDeviceModal() {
  if (!draft.value) return;
  deviceForm.value = {
    type: "风机盘管",
    name: "",
    floorId: activeNode.value?.floor.id ?? fallbackFloorId(),
    quantity: 1,
  };
  deviceModalOpen.value = true;
}

/** 弹层确认新增设备：型号留空走默认 ID，选了参数库型号则带出常用额定值。 */
function onConfirmAddDevice() {
  if (!draft.value) return;
  const result = addDevice(draft.value);
  const form = deviceForm.value;
  const catalog = selectedCatalogItem.value;
  const patch: Partial<DeviceInput> = {
    type: form.type,
    name: form.name || null,
    floor_id: form.floorId || null,
    quantity: Number.isFinite(form.quantity) && form.quantity >= 1 ? Math.round(form.quantity) : 1,
    capacity_kw: catalog?.额定容量 ?? null,
    flow_m3_h: catalog?.额定流量 ?? null,
    head_m: catalog?.额定扬程 ?? null,
    power_kw: catalog?.输入功率 ?? null,
  };
  draft.value = updateDeviceAt(result.project, draft.value.devices.length, patch);
  deviceModalOpen.value = false;
  notice.value = `已新增设备 ${result.deviceId}${form.name ? `（${form.name}）` : ""}，额定参数来自参数库，可留空让后端解析。`;
}

function onPatchDevice(index: number, patch: Partial<DeviceInput>) {
  if (draft.value) draft.value = updateDeviceAt(draft.value, index, patch);
}

function onRemoveDevice(index: number) {
  if (!draft.value) return;
  const deviceId = draft.value.devices[index]?.id || `第 ${index + 1} 行`;
  draft.value = removeDeviceAt(draft.value, index);
  notice.value = `已删除设备 ${deviceId}，相关房间绑定和连接已清理。`;
}

function onAddPipe() {
  if (!draft.value) return;
  const result = addPipe(draft.value);
  draft.value = result.project;
  const index = draft.value.pipes.length - 1;
  if (isRiserView.value) {
    // 立管视图里新增的管路默认为立管供水并留在本视图
    draft.value = updatePipeAt(draft.value, index, { pipe_type: "riser_supply" });
  } else if (activeNode.value) {
    draft.value = updatePipeAt(draft.value, index, { floor_id: activeNode.value.floor.id });
  } else {
    // 没选中楼层节点时显式落到 fallbackFloorId()，避免新管路在楼层汇总里
    // 归到与界面显示不同的层（后端兜底与它同序，但显式写出更直观）。
    const floorId = fallbackFloorId();
    if (floorId) draft.value = updatePipeAt(draft.value, index, { floor_id: floorId });
  }
  notice.value = `已新增管路 ${result.pipeId}，保存后生效。`;
}

function onPatchPipe(index: number, patch: Partial<PipeInput>) {
  if (draft.value) draft.value = updatePipeAt(draft.value, index, patch);
}

function onRemovePipe(index: number) {
  if (!draft.value) return;
  const pipeId = draft.value.pipes[index]?.id || `第 ${index + 1} 行`;
  draft.value = removePipeAt(draft.value, index);
  notice.value = `已删除管路 ${pipeId}，相关连接已清理。`;
}

function onAddConnection() {
  if (!draft.value) return;
  const result = addConnection(draft.value);
  draft.value = result.project;
  notice.value = "已新增连接，请确认起点、终点和系统。";
}

function onPatchConnection(index: number, patch: Partial<ConnectionInput>) {
  if (draft.value) draft.value = updateConnection(draft.value, index, patch);
}

function onRemoveConnection(index: number) {
  if (!draft.value) return;
  draft.value = removeConnection(draft.value, index);
  notice.value = "已删除连接。";
}

function inputText(event: Event): string {
  return (event.target as HTMLInputElement).value;
}

function inputNumber(event: Event): number {
  const value = inputText(event).trim();
  return value ? Number(value) : Number.NaN;
}

function optionalNumber(event: Event): number | null {
  const value = inputText(event).trim();
  return value ? Number(value) : null;
}

function valueOrBlank(value: number | null | undefined): number | string {
  return value === null || value === undefined ? "" : value;
}

/** 空气管路按风管规格显示（宽×高），水路显示管径。 */
function pipeSizeText(pipe: PipeInput): string {
  if (pipe.system === "air") {
    if (pipe.width_mm && pipe.height_mm) return `${Math.round(pipe.width_mm)}×${Math.round(pipe.height_mm)}`;
    return "--";
  }
  return pipe.diameter_mm ? `DN${Math.round(pipe.diameter_mm)}` : "--";
}

async function save() {
  if (!draft.value || !canSave.value) return;
  saved.value = false;
  notice.value = "";
  if (await project.updateProject(draft.value)) saved.value = true;
}
</script>

<template>
  <div class="view-stack building-editor" :class="{ 'is-readonly': project.usingSample }">
    <section class="view-heading action-heading">
      <div>
        <p class="eyebrow">CLASSROOM SYSTEM BUILDER</p>
        <h1>结构建模</h1>
        <p>按楼层搭建房间、设备、管路和连接，保存后重新运行评估或仿真</p>
      </div>
      <div class="toolbar">
        <span v-if="dirty" class="draft-indicator">有未保存修改</span>
        <button class="button secondary" :disabled="!editable" @click="onAddFloor"><Plus />新增楼层</button>
        <button class="button secondary" :disabled="project.saving || !dirty" @click="resetDraft"><RotateCcw />还原</button>
        <button class="button primary" :disabled="project.saving || !canSave" @click="save">
          <Save />{{ project.saving ? "保存中" : "保存结构" }}
        </button>
      </div>
    </section>

    <div v-if="project.usingSample" class="notice-banner">样例项目不能直接编辑，先保存为项目再改结构。当前显示只读预览，方便在保存前理解楼层、房间和管网关系。</div>
    <div v-if="realtime.running" class="warning-banner">仿真正在运行，停止仿真后可编辑项目结构。</div>
    <div v-if="!draft" class="empty-state">尚未选择项目。</div>

    <template v-else>
      <section class="metric-grid" aria-label="结构摘要">
        <article class="metric-cell"><Layers /><span>楼层</span><strong>{{ tree.nodes.length }} 层</strong></article>
        <article class="metric-cell"><Network /><span>房间</span><strong>{{ roomCount }} 间</strong></article>
        <article class="metric-cell"><Building2 /><span>设备</span><strong>{{ devices.length }} 台</strong></article>
        <article class="metric-cell"><Cable /><span>管路 / 连接</span><strong>{{ pipes.length }} / {{ connections.length }}</strong></article>
        <article class="metric-cell"><span>总面积</span><strong>{{ finite(totalArea, 2) }} m²</strong></article>
      </section>

      <div v-if="!areasValid" class="error-banner">每个房间的面积必须为大于 0 的数值。</div>
      <div v-else-if="!namesValid" class="error-banner">房间名称不能为空。</div>
      <div v-else-if="validation.issues.length" class="editor-validation" role="alert">
        <strong>保存前请修正 {{ validation.issues.length }} 项结构输入：</strong>
        <ul>
          <li v-for="issue in validation.issues" :key="`${issue.code}-${issue.field}-${issue.target}`">{{ issue.message }}</li>
        </ul>
      </div>
      <div v-else-if="project.saveError" class="error-banner">保存失败：{{ project.saveError }}</div>
      <div v-else-if="saved && !dirty" class="empty-state good">结构已保存。</div>
      <div v-else-if="notice" class="empty-state">{{ notice }}</div>

      <div v-if="!tree.nodes.length" class="empty-state large">还没有楼层，点「新增楼层」开始搭建。</div>

      <div v-else class="builder-layout">
        <!-- 左侧：楼层导航 -->
        <aside class="floor-nav" aria-label="楼层导航">
          <button
            v-for="node in tree.nodes"
            :key="node.floor.id"
            class="floor-nav-item"
            :class="{ active: !isRiserView && node.floor.id === activeNode?.floor.id }"
            @click="selectFloor(node.floor.id)"
          >
            <span class="floor-nav-name">{{ node.floor.name || node.floor.id }}</span>
            <span class="floor-nav-meta">{{ node.floor.id }} · {{ node.rooms.length }} 房间</span>
          </button>
          <button
            v-if="riserPipes.length"
            class="floor-nav-item floor-nav-riser"
            :class="{ active: isRiserView }"
            @click="selectFloor(RISER_VIEW)"
          >
            <span class="floor-nav-name">立管 / 全楼共用</span>
            <span class="floor-nav-meta">{{ riserPipes.length }} 根 · 贯穿多层</span>
          </button>
          <fieldset v-if="tree.orphans.length" class="orphan-block" :disabled="!editable">
            <p class="table-note">下列房间的楼层不存在，评估会报 UNKNOWN_ROOM_FLOOR。请为其选择楼层。</p>
            <div v-for="item in tree.orphans" :key="item.key" class="tree-room orphan-room">
              <strong>{{ item.room.name }}</strong>
              <span class="tree-meta">floor_id：{{ item.room.floor_id || "（空）" }}</span>
              <label class="tree-field">
                <span>改为</span>
                <select value="" :aria-label="`${item.room.name} 改为所属楼层`" @change="onPatchRoom(item.key, { floor_id: inputText($event) })">
                  <option value="" disabled>选择楼层</option>
                  <option v-for="node2 in tree.nodes" :key="node2.floor.id" :value="node2.floor.id">{{ node2.floor.name || node2.floor.id }}</option>
                </select>
              </label>
              <button class="icon-button danger" :aria-label="`删除房间 ${item.room.name}`" @click="onRemoveRoom(item.key, item.room.name)"><Trash2 /></button>
            </div>
          </fieldset>
        </aside>

        <!-- 右侧：立管视图（跨层输配） -->
        <fieldset v-if="isRiserView" class="floor-detail builder-fields" :disabled="!editable">
          <section class="band-section floor-head">
            <div class="section-title floor-head-title">
              <div>
                <p class="eyebrow">RISERS</p>
                <h2>立管 / 全楼共用</h2>
                <p class="editor-note">{{ riserPipes.length }} 根立管 · {{ riserConnections.length }} 条连接。立管贯穿多层，不归属单一楼层；这里集中管理垂直输配及其跨层连接。</p>
              </div>
            </div>
          </section>

          <section class="band-section" aria-labelledby="riser-pipes-title">
            <div class="section-title">
              <div><p class="eyebrow">NETWORK SEGMENTS</p><h2 id="riser-pipes-title">立管（{{ riserPipes.length }}）</h2><p class="editor-note">改动「所属楼层」并选某一层后，该管段会移入那一层的管段列表。</p></div>
              <button class="button secondary" :disabled="project.saving" @click="onAddPipe"><Plus />新增管路</button>
            </div>
            <div v-if="!riserPipes.length" class="empty-state">当前没有立管。</div>
            <div v-else class="table-wrap">
              <table class="editor-table pipe-editor-table">
                <thead><tr><th>ID</th><th>系统</th><th>类型</th><th>长度（m）</th><th>规格</th><th></th></tr></thead>
                <tbody>
                  <template v-for="{ pipe, index } in riserPipes" :key="`${pipe.id}-${index}`">
                    <tr :class="{ 'editor-row-expanded': expandedPipeIds.has(`${pipe.id}-${index}`) }">
                      <td>
                        <div class="device-cell">
                          <button class="editor-expand" :aria-expanded="expandedPipeIds.has(`${pipe.id}-${index}`)" :aria-label="`${pipe.id} 详情`" @click="togglePipeRow(`${pipe.id}-${index}`)"><ChevronRight class="editor-chevron" /></button>
                          <input :value="pipe.id" class="editor-id-input" :aria-label="`管路 ${index + 1} ID`" @input="onPatchPipe(index, { id: inputText($event) })" />
                        </div>
                      </td>
                      <td><select :value="pipe.system" :aria-label="`${pipe.id} 系统`" @change="onPatchPipe(index, { system: inputText($event) })"><option v-for="system in HVAC_SYSTEMS" :key="system" :value="system">{{ SYSTEM_LABELS[system] }}</option></select></td>
                      <td><select :value="pipe.pipe_type ?? 'unknown'" :aria-label="`${pipe.id} 管路类型`" @change="onPatchPipe(index, { pipe_type: inputText($event) })"><option v-for="pipeType in PIPE_TYPES" :key="pipeType" :value="pipeType">{{ PIPE_TYPE_LABELS[pipeType] }}</option></select></td>
                      <td><input :value="pipe.length_m" type="number" min="0" step="0.1" class="editor-num" :aria-label="`${pipe.id} 长度`" @input="onPatchPipe(index, { length_m: inputNumber($event) })" /></td>
                      <td><span class="pipe-size-cell">{{ pipeSizeText(pipe) }}</span></td>
                      <td><button class="icon-button danger" :aria-label="`删除管路 ${pipe.id}`" @click="onRemovePipe(index)"><Trash2 /></button></td>
                    </tr>
                    <tr v-if="expandedPipeIds.has(`${pipe.id}-${index}`)" class="editor-detail-row">
                      <td colspan="6">
                        <div class="editor-detail-grid">
                          <template v-if="pipe.system === 'air'">
                            <label>宽度（mm）<input :value="valueOrBlank(pipe.width_mm)" type="number" min="0" step="1" :aria-label="`${pipe.id} 宽度`" @input="onPatchPipe(index, { width_mm: optionalNumber($event) })" /></label>
                            <label>高度（mm）<input :value="valueOrBlank(pipe.height_mm)" type="number" min="0" step="1" :aria-label="`${pipe.id} 高度`" @input="onPatchPipe(index, { height_mm: optionalNumber($event) })" /></label>
                          </template>
                          <template v-else>
                            <label>管径（mm）<input :value="valueOrBlank(pipe.diameter_mm)" type="number" min="0" step="1" :aria-label="`${pipe.id} 管径`" @input="onPatchPipe(index, { diameter_mm: optionalNumber($event) })" /></label>
                          </template>
                          <label>所属楼层<select :value="pipe.floor_id ?? ''" :aria-label="`${pipe.id} 所属楼层`" @change="onPatchPipe(index, { floor_id: inputText($event) || null })"><option value="">未指定（留在立管视图）</option><option v-for="floor in draft.floors" :key="floor.id" :value="floor.id">{{ floor.name || floor.id }}</option></select></label>
                          <label>流量（m³/h，留空自动算）<input :value="valueOrBlank(pipe.flow_m3_h)" type="number" min="0" step="0.1" :aria-label="`${pipe.id} 流量`" @input="onPatchPipe(index, { flow_m3_h: optionalNumber($event) })" /></label>
                          <label>局部阻力系数<input :value="valueOrBlank(pipe.local_resistance_coefficient)" type="number" min="0" step="0.1" :aria-label="`${pipe.id} 局部阻力系数`" @input="onPatchPipe(index, { local_resistance_coefficient: optionalNumber($event) })" /></label>
                        </div>
                      </td>
                    </tr>
                  </template>
                </tbody>
              </table>
            </div>
          </section>

          <section class="band-section" aria-labelledby="riser-connections-title">
            <div class="section-title">
              <div><p class="eyebrow">TOPOLOGY</p><h2 id="riser-connections-title">跨层连接（{{ riserConnections.length }} / {{ connections.length }}）</h2><p class="editor-note">起终点任一为立管的连接都在这里。</p></div>
              <button class="button secondary" :disabled="project.saving" @click="onAddConnection"><Plus />新增连接</button>
            </div>
            <div v-if="!riserConnections.length" class="empty-state">没有涉及立管的连接。</div>
            <div v-else class="table-wrap">
              <table class="editor-table connection-editor-table">
                <thead><tr><th>#</th><th>系统</th><th>起点</th><th>终点</th><th></th></tr></thead>
                <tbody>
                  <tr v-for="{ connection, index } in riserConnections" :key="`${index}-${connection.from_id}-${connection.to_id}`">
                    <td><span class="tree-meta">{{ index + 1 }}</span></td>
                    <td><select :value="connection.system" :aria-label="`连接 ${index + 1} 系统`" @change="onPatchConnection(index, { system: inputText($event) })"><option v-for="system in HVAC_SYSTEMS" :key="system" :value="system">{{ SYSTEM_LABELS[system] }}</option></select></td>
                    <td>
                      <select :value="connection.from_id" :aria-label="`连接 ${index + 1} 起点`" @change="onPatchConnection(index, { from_id: inputText($event) })">
                        <option v-for="node in nodeOptions" :key="`from-${node.kind}-${node.id}`" :value="node.id">{{ node.label }}</option>
                        <option v-if="connection.from_id && !nodeOptions.some((n) => n.id === connection.from_id)" :value="connection.from_id">{{ connection.from_id }}（未知节点）</option>
                      </select>
                    </td>
                    <td>
                      <select :value="connection.to_id" :aria-label="`连接 ${index + 1} 终点`" @change="onPatchConnection(index, { to_id: inputText($event) })">
                        <option v-for="node in nodeOptions" :key="`to-${node.kind}-${node.id}`" :value="node.id">{{ node.label }}</option>
                        <option v-if="connection.to_id && !nodeOptions.some((n) => n.id === connection.to_id)" :value="connection.to_id">{{ connection.to_id }}（未知节点）</option>
                      </select>
                    </td>
                    <td><button class="icon-button danger" :aria-label="`删除连接 ${index + 1}`" @click="onRemoveConnection(index)"><Trash2 /></button></td>
                  </tr>
                </tbody>
              </table>
            </div>
          </section>
        </fieldset>

        <!-- 右侧：选中楼层详情 -->
        <div v-if="activeNode && !isRiserView" class="floor-detail">
          <nav class="workspace-tabs builder-tabs" role="tablist" aria-label="楼层内容"><button v-for="section in sections" :key="section.id" type="button" role="tab" :aria-selected="activeSection === section.id" @click="activeSection = section.id">{{ section.label }}</button></nav>
          <fieldset class="builder-fields" :disabled="!editable">
          <section class="band-section floor-head">
            <div class="section-title floor-head-title">
              <div>
                <p class="eyebrow">FLOOR</p>
                <h2>{{ activeNode.floor.name || activeNode.floor.id }}</h2>
                <p class="editor-note">{{ activeNode.floor.id }} · 层高 {{ finite(activeNode.floor.height_m, 1) }} m · 标高 {{ finite(activeNode.floor.elevation_m, 2) }} m · {{ activeNode.rooms.length }} 房间 · {{ floorDevices.length }} 设备 · {{ floorPipes.length }} 管段</p>
              </div>
              <div class="toolbar">
                <input class="floor-name-input" :value="activeNode.floor.name ?? ''" :aria-label="`${activeNode.floor.id} 楼层名称`" placeholder="楼层名称" @input="onRenameFloor(activeNode.floor.id, inputText($event))" />
                <button class="button secondary" @click="onAddRoom(activeNode.floor.id)"><Plus />房间</button>
                <button class="icon-button danger" :disabled="!editable" :aria-label="`删除 ${activeNode.floor.name || activeNode.floor.id}`" @click="onRemoveFloor(activeNode.floor.id, activeNode.floor.name || activeNode.floor.id)"><Trash2 /></button>
              </div>
            </div>
          </section>

          <!-- 房间卡片网格 -->
          <section v-show="activeSection === 'rooms'" class="band-section" aria-labelledby="floor-rooms-title">
            <div class="section-title"><div><p class="eyebrow">ROOMS</p><h2 id="floor-rooms-title">房间</h2></div><Network /></div>
            <div v-if="!activeNode.rooms.length" class="empty-state">该层还没有房间，点上方「房间」新增。</div>
            <div v-else class="room-grid">
              <article v-for="item in activeNode.rooms" :key="item.key" class="room-card">
                <header class="room-card-head">
                  <input class="room-card-name" :value="item.room.name" :aria-label="`${item.key} 名称`" placeholder="房间名称" @input="onPatchRoom(item.key, { name: inputText($event) })" />
                  <button class="icon-button danger" :aria-label="`删除房间 ${item.room.name}`" @click="onRemoveRoom(item.key, item.room.name)"><Trash2 /></button>
                </header>
                <div class="room-card-fields">
                  <label>面积（m²）<input :value="item.room.area_m2" type="number" min="0.1" step="1" :aria-label="`${item.room.name} 面积（m²）`" :aria-invalid="!(Number.isFinite(item.room.area_m2) && item.room.area_m2 > 0)" @input="onPatchRoom(item.key, { area_m2: inputNumber($event) })" /></label>
                  <label>人数<input :value="item.room.people" type="number" min="0" step="1" :aria-label="`${item.room.name} 人数`" @input="onPatchRoom(item.key, { people: inputNumber($event) })" /></label>
                  <label>用途
                    <select :value="item.room.purpose ?? 'office'" :aria-label="`${item.room.name} 用途`" @change="onPatchRoom(item.key, { purpose: inputText($event) })">
                      <option v-for="option in ROOM_PURPOSE_OPTIONS" :key="option.value" :value="option.value">{{ option.label }}（{{ option.summary }}）</option>
                      <option v-if="item.room.purpose && !ROOM_PURPOSE_OPTIONS.some((o) => o.value === item.room.purpose)" :value="item.room.purpose">{{ item.room.purpose }}（当前）</option>
                    </select>
                  </label>
                  <label>所属楼层
                    <select :value="item.room.floor_id ?? ''" :aria-label="`${item.room.name} 所属楼层`" @change="onPatchRoom(item.key, { floor_id: inputText($event) })">
                      <option v-for="node2 in tree.nodes" :key="node2.floor.id" :value="node2.floor.id">{{ node2.floor.name || node2.floor.id }}</option>
                    </select>
                  </label>
                </div>
                <div class="room-card-terminals">
                  <span class="room-card-label">服务末端</span>
                  <div v-if="terminalDeviceList.length" class="terminal-chip-row">
                    <button
                      v-for="terminal in terminalDeviceList.filter((d) => (item.room.device_ids ?? []).includes(d.id))"
                      :key="terminal.id"
                      class="terminal-chip"
                      :title="`点击解绑 ${terminal.id}`"
                      @click="onUnbindDevice(item.key, terminal.id)"
                    >{{ terminal.name || terminal.type || terminal.id }}<small>{{ terminal.id }}</small></button>
                    <select class="terminal-add" value="" :aria-label="`${item.room.name} 添加末端`" @change="(item.room.device_ids ?? []).includes(inputText($event)) ? null : onPatchRoom(item.key, { device_ids: [...(item.room.device_ids ?? []), inputText($event)] })">
                      <option value="" disabled>＋ 绑定末端</option>
                      <option v-for="terminal in terminalDeviceList.filter((d) => !(item.room.device_ids ?? []).includes(d.id))" :key="terminal.id" :value="terminal.id">{{ terminal.name || terminal.type || terminal.id }} · {{ terminal.id }}</option>
                    </select>
                  </div>
                  <span v-else class="tree-meta">暂无可服务末端（先新增 FCU / AHU）</span>
                  <span class="tree-meta room-key-hint">{{ item.key }}</span>
                </div>
              </article>
            </div>
          </section>

          <!-- 本层设备（紧凑列表 + 行内详情） -->
          <section v-show="activeSection === 'devices'" class="band-section" aria-labelledby="floor-devices-title">
            <div class="section-title">
              <div><p class="eyebrow">EQUIPMENT</p><h2 id="floor-devices-title">设备（{{ floorDevices.length }}）</h2><p class="editor-note">额定值可留空由后端解析；房间「服务末端」只从 FCU、AHU 和新风机组中选择。</p></div>
              <button class="button secondary" :disabled="project.saving" @click="openDeviceModal"><Plus />新增设备</button>
            </div>
            <div v-if="!floorDevices.length" class="empty-state">本层没有设备。点「新增设备」后即可在房间中绑定末端。</div>
            <div v-else class="table-wrap">
              <table class="editor-table device-editor-table">
                <thead><tr><th>型号</th><th>类型</th><th>数量</th><th>安装位置</th><th></th></tr></thead>
                <tbody>
                  <template v-for="{ device, index } in floorDevices" :key="`${device.id}-${index}`">
                    <tr :class="{ 'editor-row-expanded': expandedDeviceIds.has(`${device.id}-${index}`) }">
                      <td>
                        <div class="device-cell">
                          <button class="editor-expand" :aria-expanded="expandedDeviceIds.has(`${device.id}-${index}`)" :aria-label="`${device.id} 详情`" @click="toggleDeviceRow(`${device.id}-${index}`)"><ChevronRight class="editor-chevron" /></button>
                          <div class="device-id-block">
                            <strong>{{ device.id }}</strong>
                            <input :value="device.name ?? ''" placeholder="名称 / 型号（留空由参数库解析）" :aria-label="`${device.id} 名称或型号`" @input="onPatchDevice(index, { name: inputText($event) || null })" />
                          </div>
                        </div>
                      </td>
                      <td><input :value="device.type" list="editor-device-types" :aria-label="`${device.id} 类型`" @input="onPatchDevice(index, { type: inputText($event) })" /></td>
                      <td><input :value="device.quantity" type="number" min="1" step="1" class="editor-num" :aria-label="`${device.id} 数量`" @input="onPatchDevice(index, { quantity: inputNumber($event) })" /></td>
                      <td><select :value="device.location_type ?? ''" :aria-label="`${device.id} 安装位置`" @change="onPatchDevice(index, { location_type: inputText($event) || null })"><option v-for="location in DEVICE_LOCATION_OPTIONS" :key="location.value || 'none'" :value="location.value">{{ location.label }}</option><option v-if="device.location_type && !isKnownDeviceLocation(device.location_type)" :value="device.location_type">{{ deviceLocationLabel(device.location_type) }}</option></select></td>
                      <td><button class="icon-button danger" :aria-label="`删除设备 ${device.id}`" @click="onRemoveDevice(index)"><Trash2 /></button></td>
                    </tr>
                    <tr v-if="expandedDeviceIds.has(`${device.id}-${index}`)" class="editor-detail-row">
                      <td colspan="5">
                        <div class="editor-detail-grid">
                          <label>ID<input :value="device.id" class="editor-id-input" :aria-label="`设备 ${index + 1} ID`" @input="onPatchDevice(index, { id: inputText($event) })" /></label>
                          <label>安装楼层<select :value="device.floor_id ?? ''" :aria-label="`${device.id} 所属楼层`" @change="onPatchDevice(index, { floor_id: inputText($event) || null })"><option value="">未指定</option><option v-for="floor in draft.floors" :key="floor.id" :value="floor.id">{{ floor.name || floor.id }}</option></select></label>
                          <label>容量（kW）<input :value="valueOrBlank(device.capacity_kw)" type="number" min="0" step="0.1" :aria-label="`${device.id} 容量`" @input="onPatchDevice(index, { capacity_kw: optionalNumber($event) })" /></label>
                          <label>流量（m³/h）<input :value="valueOrBlank(device.flow_m3_h)" type="number" min="0" step="0.1" :aria-label="`${device.id} 流量`" @input="onPatchDevice(index, { flow_m3_h: optionalNumber($event) })" /></label>
                          <label>扬程（m）<input :value="valueOrBlank(device.head_m)" type="number" min="0" step="0.1" :aria-label="`${device.id} 扬程`" @input="onPatchDevice(index, { head_m: optionalNumber($event) })" /></label>
                          <label>功率（kW）<input :value="valueOrBlank(device.power_kw)" type="number" min="0" step="0.1" :aria-label="`${device.id} 功率`" @input="onPatchDevice(index, { power_kw: optionalNumber($event) })" /></label>
                        </div>
                      </td>
                    </tr>
                  </template>
                </tbody>
              </table>
            </div>
            <datalist id="editor-device-types"><option v-for="type in DEVICE_TYPE_OPTIONS" :key="type" :value="type" /></datalist>
          </section>

          <!-- 本层管路（5 列 + 行内详情） -->
          <section v-show="activeSection === 'network'" class="band-section" aria-labelledby="floor-pipes-title">
            <div class="section-title">
              <div><p class="eyebrow">NETWORK SEGMENTS</p><h2 id="floor-pipes-title">管段（{{ floorPipes.length }}）</h2><p class="editor-note">水管填管径，空气管填宽×高（详情里）；尺寸最终由后端校验。</p></div>
              <button class="button secondary" :disabled="project.saving" @click="onAddPipe"><Plus />新增管路</button>
            </div>
            <div v-if="!floorPipes.length" class="empty-state">本层没有管段。新增管路后在连接列表组合系统拓扑。</div>
            <div v-else class="table-wrap">
              <table class="editor-table pipe-editor-table">
                <thead><tr><th>ID</th><th>系统</th><th>类型</th><th>长度（m）</th><th>规格</th><th></th></tr></thead>
                <tbody>
                  <template v-for="{ pipe, index } in floorPipes" :key="`${pipe.id}-${index}`">
                    <tr :class="{ 'editor-row-expanded': expandedPipeIds.has(`${pipe.id}-${index}`) }">
                      <td>
                        <div class="device-cell">
                          <button class="editor-expand" :aria-expanded="expandedPipeIds.has(`${pipe.id}-${index}`)" :aria-label="`${pipe.id} 详情`" @click="togglePipeRow(`${pipe.id}-${index}`)"><ChevronRight class="editor-chevron" /></button>
                          <input :value="pipe.id" class="editor-id-input" :aria-label="`管路 ${index + 1} ID`" @input="onPatchPipe(index, { id: inputText($event) })" />
                        </div>
                      </td>
                      <td><select :value="pipe.system" :aria-label="`${pipe.id} 系统`" @change="onPatchPipe(index, { system: inputText($event) })"><option v-for="system in HVAC_SYSTEMS" :key="system" :value="system">{{ SYSTEM_LABELS[system] }}</option></select></td>
                      <td><select :value="pipe.pipe_type ?? 'unknown'" :aria-label="`${pipe.id} 管路类型`" @change="onPatchPipe(index, { pipe_type: inputText($event) })"><option v-for="pipeType in PIPE_TYPES" :key="pipeType" :value="pipeType">{{ PIPE_TYPE_LABELS[pipeType] }}</option></select></td>
                      <td><input :value="pipe.length_m" type="number" min="0" step="0.1" class="editor-num" :aria-label="`${pipe.id} 长度`" @input="onPatchPipe(index, { length_m: inputNumber($event) })" /></td>
                      <td><span class="pipe-size-cell">{{ pipeSizeText(pipe) }}</span></td>
                      <td><button class="icon-button danger" :aria-label="`删除管路 ${pipe.id}`" @click="onRemovePipe(index)"><Trash2 /></button></td>
                    </tr>
                    <tr v-if="expandedPipeIds.has(`${pipe.id}-${index}`)" class="editor-detail-row">
                      <td colspan="6">
                        <div class="editor-detail-grid">
                          <template v-if="pipe.system === 'air'">
                            <label>宽度（mm）<input :value="valueOrBlank(pipe.width_mm)" type="number" min="0" step="1" :aria-label="`${pipe.id} 宽度`" @input="onPatchPipe(index, { width_mm: optionalNumber($event) })" /></label>
                            <label>高度（mm）<input :value="valueOrBlank(pipe.height_mm)" type="number" min="0" step="1" :aria-label="`${pipe.id} 高度`" @input="onPatchPipe(index, { height_mm: optionalNumber($event) })" /></label>
                          </template>
                          <template v-else>
                            <label>管径（mm）<input :value="valueOrBlank(pipe.diameter_mm)" type="number" min="0" step="1" :aria-label="`${pipe.id} 管径`" @input="onPatchPipe(index, { diameter_mm: optionalNumber($event) })" /></label>
                          </template>
                          <label>所属楼层<select :value="pipe.floor_id ?? ''" :aria-label="`${pipe.id} 所属楼层`" @change="onPatchPipe(index, { floor_id: inputText($event) || null })"><option value="">未指定</option><option v-for="floor in draft.floors" :key="floor.id" :value="floor.id">{{ floor.name || floor.id }}</option></select></label>
                          <label>流量（m³/h，留空自动算）<input :value="valueOrBlank(pipe.flow_m3_h)" type="number" min="0" step="0.1" :aria-label="`${pipe.id} 流量`" @input="onPatchPipe(index, { flow_m3_h: optionalNumber($event) })" /></label>
                          <label>局部阻力系数<input :value="valueOrBlank(pipe.local_resistance_coefficient)" type="number" min="0" step="0.1" :aria-label="`${pipe.id} 局部阻力系数`" @input="onPatchPipe(index, { local_resistance_coefficient: optionalNumber($event) })" /></label>
                        </div>
                      </td>
                    </tr>
                  </template>
                </tbody>
              </table>
            </div>
          </section>

          <!-- 本层连接（分组下拉） -->
          <section v-show="activeSection === 'network'" class="band-section" aria-labelledby="floor-connections-title">
            <div class="section-title">
              <div><p class="eyebrow">TOPOLOGY</p><h2 id="floor-connections-title">连接（{{ floorConnections.length }} / {{ connections.length }}）</h2><p class="editor-note">只显示与本层相关的连接；下拉按楼层分组，无需记 ID。</p></div>
              <button class="button secondary" :disabled="project.saving" @click="onAddConnection"><Plus />新增连接</button>
            </div>
            <div v-if="!connections.length" class="empty-state">当前没有连接。新增连接后选择起点、终点和介质系统。</div>
            <div v-else class="table-wrap">
              <table class="editor-table connection-editor-table">
                <thead><tr><th>#</th><th>系统</th><th>起点</th><th>终点</th><th></th></tr></thead>
                <tbody>
                  <tr v-for="{ connection, index } in floorConnections" :key="`${index}-${connection.from_id}-${connection.to_id}`">
                    <td><span class="tree-meta">{{ index + 1 }}</span></td>
                    <td><select :value="connection.system" :aria-label="`连接 ${index + 1} 系统`" @change="onPatchConnection(index, { system: inputText($event) })"><option v-for="system in HVAC_SYSTEMS" :key="system" :value="system">{{ SYSTEM_LABELS[system] }}</option></select></td>
                    <td>
                      <select :value="connection.from_id" :aria-label="`连接 ${index + 1} 起点`" @change="onPatchConnection(index, { from_id: inputText($event) })">
                        <option v-for="node in nodeOptions" :key="`from-${node.kind}-${node.id}`" :value="node.id">{{ node.label }}</option>
                        <option v-if="connection.from_id && !nodeOptions.some((n) => n.id === connection.from_id)" :value="connection.from_id">{{ connection.from_id }}（未知节点）</option>
                      </select>
                    </td>
                    <td>
                      <select :value="connection.to_id" :aria-label="`连接 ${index + 1} 终点`" @change="onPatchConnection(index, { to_id: inputText($event) })">
                        <option v-for="node in nodeOptions" :key="`to-${node.kind}-${node.id}`" :value="node.id">{{ node.label }}</option>
                        <option v-if="connection.to_id && !nodeOptions.some((n) => n.id === connection.to_id)" :value="connection.to_id">{{ connection.to_id }}（未知节点）</option>
                      </select>
                    </td>
                    <td><button class="icon-button danger" :aria-label="`删除连接 ${index + 1}`" @click="onRemoveConnection(index)"><Trash2 /></button></td>
                  </tr>
                </tbody>
              </table>
            </div>
          </section>
          </fieldset>
        </div>
      </div>

      <!-- 新增设备弹层 -->
      <div v-if="deviceModalOpen" class="modal-backdrop" @click.self="deviceModalOpen = false">
        <div class="modal-card" role="dialog" aria-label="新增设备">
          <header class="modal-head">
            <h3>新增设备</h3>
            <button class="icon-button" aria-label="关闭" @click="deviceModalOpen = false">×</button>
          </header>
          <div class="modal-body">
            <label>设备类型
              <select v-model="deviceForm.type" aria-label="新增设备类型">
                <option v-for="type in DEVICE_TYPE_OPTIONS" :key="type" :value="type">{{ type }}</option>
              </select>
            </label>
            <label>型号（来自参数库{{ catalogLoaded ? "" : "，加载中" }}）
              <select v-model="deviceForm.name" aria-label="新增设备型号">
                <option value="">留空（不指定型号）</option>
                <option v-if="deviceForm.name && !catalogModelOptions.includes(deviceForm.name)" :value="deviceForm.name">{{ deviceForm.name }}（当前）</option>
                <option v-for="model in catalogModelOptions" :key="model" :value="model">{{ model }}</option>
              </select>
            </label>
            <label>安装楼层
              <select v-model="deviceForm.floorId" aria-label="新增设备楼层">
                <option v-for="floor in draft.floors" :key="floor.id" :value="floor.id">{{ floor.name || floor.id }}</option>
              </select>
            </label>
            <label>数量<input v-model.number="deviceForm.quantity" type="number" min="1" step="1" aria-label="新增设备数量" /></label>
            <div v-if="selectedCatalogItem" class="modal-catalog-hint">
              <strong>{{ deviceForm.name }}</strong>
              <span v-if="selectedCatalogItem.额定容量">额定容量 {{ finite(selectedCatalogItem.额定容量, 1) }} kW</span>
              <span v-if="selectedCatalogItem.额定流量">额定流量 {{ finite(selectedCatalogItem.额定流量, 1) }} m³/h</span>
              <span v-if="selectedCatalogItem.输入功率">输入功率 {{ finite(selectedCatalogItem.输入功率, 1) }} kW</span>
              <span class="tree-meta">选型后这些额定值会带入设备详情，可再修改。</span>
            </div>
          </div>
          <footer class="modal-foot">
            <button class="button secondary" @click="deviceModalOpen = false">取消</button>
            <button class="button primary" @click="onConfirmAddDevice"><Plus />添加</button>
          </footer>
        </div>
      </div>
    </template>
  </div>
</template>

<style scoped>
.builder-fields { min-width: 0; border: 0; padding: 0; margin: 0; }
.builder-fields:disabled input, .builder-fields:disabled select { opacity: 1; background: var(--surface-soft); color: var(--ink); }
.builder-fields:disabled button { opacity: .4; }
.builder-tabs { padding: 12px 18px; }
.draft-indicator { font-size: 12px; color: #9a661c; }
.building-editor .editor-note { margin-top: 5px; color: var(--muted); font-size: 11px; font-weight: 500; line-height: 1.5; }
.building-editor .editor-validation { padding: 14px 18px; color: #7d3d32; background: #fff3ed; border-bottom: 1px solid #e9c2b4; font-size: 12px; }
.building-editor .editor-validation ul { margin: 8px 0 0 18px; display: grid; gap: 4px; }
.building-editor .editor-table { min-width: 760px; }
.building-editor .device-editor-table { min-width: 820px; }
.building-editor .pipe-editor-table { min-width: 780px; }
.building-editor .connection-editor-table { min-width: 720px; }
.building-editor .editor-table input, .building-editor .editor-table select { width: 100%; min-width: 90px; height: 34px; padding: 0 7px; font-size: 11px; }
.building-editor .editor-table .editor-id-input { min-width: 150px; font-family: "Bahnschrift", "Microsoft YaHei", sans-serif; }
.building-editor .editor-num { max-width: 76px; }
.building-editor .metric-grid { grid-template-columns: repeat(5, minmax(0, 1fr)); }

/* 双栏布局 */
.building-editor .builder-layout { display: grid; grid-template-columns: 200px minmax(0, 1fr); align-items: start; }
.floor-nav { display: grid; gap: 6px; position: sticky; top: 90px; }
.floor-nav-item { display: grid; gap: 4px; padding: 12px 14px; text-align: left; background: var(--surface); border: 1px solid var(--line); border-radius: 8px; cursor: pointer; transition: border-color .15s ease, background .15s ease; }
.floor-nav-item:hover { border-color: #9dc3c6; }
.floor-nav-item.active { border-color: var(--teal); background: #e8f4f2; box-shadow: inset 3px 0 0 var(--teal); }
.floor-nav-name { font-size: 13px; font-weight: 650; color: var(--ink); }
.floor-nav-item.active .floor-nav-name { color: var(--teal); }
.floor-nav-meta { font-size: 11px; color: var(--muted); }
.floor-nav-riser { border-style: dashed; }
.floor-nav-riser.active { background: #f4f1e8; box-shadow: inset 3px 0 0 #b08a3e; }
.floor-nav-riser.active .floor-nav-name { color: #8a6a2c; }

.floor-head-title { align-items: flex-start; }
.floor-name-input { width: 150px; height: 34px; padding: 0 10px; font-size: 12px; }

/* 房间卡片网格 */
.room-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 12px; }
.room-card { display: grid; gap: 10px; padding: 14px; background: var(--surface); border: 1px solid var(--line); border-radius: 8px; }
.room-card-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.room-card-name { flex: 1; height: 32px; padding: 0 8px; font-size: 13px; font-weight: 650; }
.room-card-fields { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 8px; }
.room-card-fields label { display: grid; gap: 4px; font-size: 11px; color: var(--muted); }
.room-card-fields input, .room-card-fields select { width: 100%; height: 30px; padding: 0 6px; font-size: 12px; }
.room-card-terminals { display: grid; gap: 6px; padding-top: 8px; border-top: 1px dashed var(--line); }
.room-card-label { font-size: 11px; color: var(--muted); }
.terminal-chip-row { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; }
.terminal-chip { display: inline-flex; align-items: baseline; gap: 6px; padding: 4px 9px; background: #e8f4f2; border: 1px solid #bfded9; border-radius: 999px; font-size: 11px; color: #23615e; cursor: pointer; }
.terminal-chip:hover { border-color: #d0906a; background: #fdf1e7; color: #8a5a33; }
.terminal-chip small { color: #6f9a97; }
.terminal-add { max-width: 220px; height: 26px; padding: 0 6px; font-size: 11px; color: var(--muted); background: transparent; border: 1px dashed #b9c9c6; border-radius: 999px; }
.room-key-hint { justify-self: end; }

/* 设备/管路行内详情 */
.device-cell { display: flex; align-items: center; gap: 8px; min-width: 0; }
.editor-expand { display: inline-grid; place-items: center; width: 22px; height: 22px; padding: 0; background: transparent; border: 1px solid var(--line); border-radius: 5px; color: var(--muted); cursor: pointer; }
.editor-expand:hover { border-color: var(--teal); color: var(--teal); }
.editor-chevron { width: 13px; height: 13px; transition: transform .15s ease; }
tr.editor-row-expanded .editor-chevron { transform: rotate(90deg); }
.editor-row-expanded > td { background: #f4f8f7; }
.editor-detail-row > td { background: #f7faf9; padding: 12px 14px; }
.editor-detail-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 10px 16px; }
.editor-detail-grid label { display: grid; gap: 4px; font-size: 11px; color: var(--muted); }
.editor-detail-grid input, .editor-detail-grid select { width: 100%; height: 30px; padding: 0 7px; font-size: 11px; }
.device-id-block { display: grid; gap: 3px; min-width: 0; }
.device-id-block strong { font-size: 12px; font-family: "Bahnschrift", "Microsoft YaHei", sans-serif; }
.device-id-block input { min-width: 170px; }
.pipe-size-cell { font-variant-numeric: tabular-nums; color: #41615f; }
.orphan-room { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; padding: 8px; background: #fff; border: 1px solid #ecc; border-radius: 6px; }

/* 弹层 */
.modal-backdrop { position: fixed; inset: 0; z-index: 60; display: grid; place-items: center; padding: 20px; background: rgba(20, 38, 41, .45); }
.modal-card { width: min(440px, 100%); background: #fff; border-radius: 10px; box-shadow: 0 24px 64px rgba(10, 30, 34, .35); overflow: hidden; }
.modal-head { display: flex; align-items: center; justify-content: space-between; padding: 14px 18px; border-bottom: 1px solid var(--line); }
.modal-head h3 { font-size: 15px; }
.modal-body { display: grid; gap: 12px; padding: 16px 18px; }
.modal-body label { display: grid; gap: 5px; font-size: 12px; color: var(--muted); }
.modal-body input, .modal-body select { height: 34px; padding: 0 8px; font-size: 12px; }
.modal-catalog-hint { display: flex; flex-wrap: wrap; align-items: baseline; gap: 6px 14px; padding: 10px 12px; background: #f0f7f5; border: 1px solid #cfe5e0; border-radius: 6px; font-size: 12px; }
.modal-catalog-hint strong { color: var(--teal); }
.modal-foot { display: flex; justify-content: flex-end; gap: 10px; padding: 12px 18px; border-top: 1px solid var(--line); }

@media (max-width: 1100px) {
  .building-editor .metric-grid { grid-template-columns: repeat(3, minmax(0, 1fr)); }
  .building-editor .builder-layout { grid-template-columns: 1fr; }
  .floor-nav { position: static; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); }
}

@media (max-width: 900px) {
  .room-card-fields { grid-template-columns: 1fr 1fr; }
}

@media (max-width: 620px) {
  .building-editor .metric-grid { grid-template-columns: 1fr; }
  .room-card-fields { grid-template-columns: 1fr; }
}
</style>
