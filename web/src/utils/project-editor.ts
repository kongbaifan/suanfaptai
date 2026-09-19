import type {
  ConnectionInput,
  DeviceInput,
  Issue,
  PipeInput,
  ProjectData,
  RoomInput,
} from "@/types";

/**
 * The editor intentionally owns only structural validation.  HVAC formulas,
 * rated-value resolution, and final Pydantic validation stay on the server.
 */

export const HVAC_SYSTEMS = [
  "chilled_water",
  "cooling_water",
  "hot_water",
  "air",
] as const;

export type HvacSystem = (typeof HVAC_SYSTEMS)[number];

export const PIPE_TYPES = [
  "main_supply",
  "main_return",
  "branch_supply",
  "branch_return",
  "terminal_branch",
  "riser_supply",
  "riser_return",
  "bypass",
  "unknown",
] as const;

export type PipeType = (typeof PIPE_TYPES)[number];

export const SYSTEM_LABELS: Record<string, string> = {
  chilled_water: "冷冻水",
  cooling_water: "冷却水",
  hot_water: "热水",
  air: "空气",
};

export const PIPE_TYPE_LABELS: Record<string, string> = {
  main_supply: "主供水",
  main_return: "主回水",
  branch_supply: "支路供水",
  branch_return: "支路回水",
  terminal_branch: "末端支路",
  riser_supply: "立管供水",
  riser_return: "立管回水",
  bypass: "旁通",
  unknown: "未指定",
};

/**
 * 可在「新增设备」弹窗中直接选取的设备类型。
 *
 * **必须与后端目录 `app/data/equipment_catalog.json` 的类型行一致**——
 * 目录里没有的类型选了也解析不出角色，`/api/v1/devices/ports` 会直接返回 404，
 * 这台设备既接不上线也拿不到参数。
 *
 * 历史缺陷：这里原有的一项 `水阀` 是**类目名**，不是设备类型。
 * `DeviceRole.WATER_VALVE` 的注释写得很清楚——"水阀（球阀、截止阀、蝶阀、闸阀、
 * 止回阀）"，那 5 个才是类型名。实测 `水阀`：目录 0 行、Unity 设备库 427 条里
 * 没有、知识库 11 条里没有、`/api/v1/devices/ports` 返回 404；而真实的 5 个
 * 水阀类型反倒不在这份清单里，用户根本选不到。本次按该注释逐个列出这 5 个。
 *
 * 已知缺口（本次**未**处理，见 CLAUDE.md）：目录里另有管件（圆直管/三通/四通/
 * 异径管/分歧管）、分集水器、测量件、`防火阀` 等类型同样不在这份清单里，
 * 用户在界面上仍无法新增。那是「可选类型清单不完整」，与本次修的
 * 「列了一个根本不存在的类型」是两回事。
 */
export const DEVICE_TYPE_OPTIONS = [
  "冷源机组",
  "空气源热泵",
  "水源热泵",
  "地源热泵",
  "循环泵",
  "冷却塔",
  "风机盘管",
  "组合式空调机组",
  "新风机组",
  "送风口",
  "回风口",
  "风阀",
  "球阀",
  "截止阀",
  "蝶阀",
  "闸阀",
  "止回阀",
] as const;

/**
 * Room service bindings and physical equipment placement are separate
 * relationships. Only these canonical terminal types may appear in the normal room
 * selector; the FastAPI role resolver remains the authoritative validator.
 */
export const ROOM_TERMINAL_DEVICE_TYPES = [
  "风机盘管",
  "组合式空调机组",
  "新风机组",
] as const;

/**
 * 历史/同义设备类型名 -> 目录现名。**必须与后端 `app/data/equipment.py` 的
 * `LEGACY_DEVICE_TYPE_ALIASES` 逐条一致**（后端是权威口径）。
 *
 * 类型改名后，此前保存的项目仍带旧类型名（例如内置样例与「中央空调示例项目」
 * 的新风机组仍是 `一体式新风机组`）。后端在解析前统一归一，所以评估一直正常；
 * 但前端若只认现名，就会把这类设备判成"不是可服务房间的末端设备"而报
 * `EDITOR_NON_TERMINAL_BINDING`——一个前端自造的假错误。
 *
 * `冷水机组` 是长期并行的同义写法（后端权威表同样收它）。它不进这张表的
 * 后果与新风机组同款：`cardRole` 在 rated 尚未加载时（`role` 为空）拿不到
 * `cold_source`，冷机卡片在实时页整块消失。
 */
const LEGACY_DEVICE_TYPE_ALIASES = new Map<string, string>([
  ["一体式新风机组", "新风机组"],
  ["冷水机组", "冷源机组"],
]);

/**
 * 把历史类型名归一为目录现名；未知类型原样返回（trim 后）。
 *
 * 用 `Map` 而不是对象字面量：对象字面量会继承 `Object.prototype`，于是
 * `aliases["constructor"]` / `["toString"]` 拿到的是继承来的函数而非 undefined，
 * `??` 不会兜底，返回值就不是字符串，调用方的 `.toLocaleLowerCase()` 直接抛错。
 * 设备类型是用户可自由填写的文本，`"constructor"` 这类值必须被当成未知类型拒绝，
 * 而不是让整个校验抛异常。
 */
export function canonicalDeviceType(deviceType: string | null | undefined): string {
  const normalized = text(deviceType);
  return LEGACY_DEVICE_TYPE_ALIASES.get(normalized) ?? normalized;
}

const NORMALIZED_ROOM_TERMINAL_TYPES = new Set(
  ROOM_TERMINAL_DEVICE_TYPES.map((type) => type.trim().toLocaleLowerCase()),
);

export const DEVICE_LOCATION_OPTIONS = [
  { value: "", label: "未指定" },
  { value: "equipment_room", label: "机房" },
  { value: "roof", label: "屋顶" },
] as const;

/** 房间用途选项：value 对应后端 PURPOSE_DEFAULTS 的键。
 *  指标摘要只做下拉提示用，权威值在后端 design_defaults.py。 */
export const ROOM_PURPOSE_OPTIONS = [
  { value: "office", label: "办公", summary: "冷 115 W/m²" },
  { value: "meeting", label: "会议室", summary: "冷 150 W/m²" },
  { value: "classroom", label: "教室", summary: "冷 140 W/m²" },
  { value: "hotel", label: "客房", summary: "冷 125 W/m²" },
  { value: "mall", label: "商场", summary: "冷 180 W/m²" },
  { value: "supermarket", label: "超市", summary: "冷 200 W/m² · 设备 30" },
  { value: "restaurant", label: "餐饮", summary: "冷 190 W/m²" },
  { value: "atrium", label: "中庭", summary: "冷 160 W/m² · 照明 25" },
  { value: "shop", label: "商铺", summary: "冷 175 W/m²" },
] as const;

export type RoomPurpose = (typeof ROOM_PURPOSE_OPTIONS)[number]["value"];

export function roomPurposeLabel(value: string | null | undefined): string {
  const normalized = text(value);
  return ROOM_PURPOSE_OPTIONS.find((option) => option.value === normalized)?.label ?? (normalized || "办公");
}

const KNOWN_DEVICE_LOCATIONS: Set<string> = new Set(
  DEVICE_LOCATION_OPTIONS.map((option) => option.value).filter(Boolean),
);

export function isRoomTerminalDevice(device: Pick<DeviceInput, "type">): boolean {
  return NORMALIZED_ROOM_TERMINAL_TYPES.has(
    canonicalDeviceType(device.type).toLocaleLowerCase(),
  );
}

export function terminalDevices(project: ProjectData): DeviceInput[] {
  return project.devices.filter(isRoomTerminalDevice);
}

export function isKnownDeviceLocation(value: string | null | undefined): boolean {
  return !text(value) || KNOWN_DEVICE_LOCATIONS.has(text(value));
}

export function deviceLocationLabel(value: string | null | undefined): string {
  const normalized = text(value);
  if (!normalized) return "未指定";
  return DEVICE_LOCATION_OPTIONS.find((option) => option.value === normalized)?.label
    ?? `其他：${normalized}`;
}

/**
 * 空白教学项目的初始结构：全部清单为空，只保留默认计算系数，
 * 由学生在建筑结构页从楼层开始逐步搭建。
 */
export function createBlankProjectData(): ProjectData {
  return {
    floors: [],
    rooms: [],
    devices: [],
    pipes: [],
    connections: [],
    simultaneity_factor: 0.9,
    safety_factor: 1.1,
    design_options: {},
  };
}

export interface ProjectDraftIssue extends Issue {
  /** Dot path into the draft, useful for focusing a field in the UI. */
  field?: string;
  entity?: "floor" | "room" | "device" | "pipe" | "connection" | "project";
}

export interface ProjectDraftValidation {
  valid: boolean;
  issues: ProjectDraftIssue[];
}

export interface EditorNodeOption {
  id: string;
  label: string;
  kind: "device" | "pipe";
  system?: string;
}

/**
 * Copy a project without structuredClone().  Pinia exposes reactive proxies,
 * which structuredClone rejects in browsers.  This copier also preserves
 * unknown room/design-option fields so an edit round-trip does not discard
 * fields owned by the backend or a future client.
 */
export function cloneProjectData(source: ProjectData): ProjectData {
  return {
    ...source,
    floors: source.floors.map((floor) => cloneRecord(floor)),
    rooms: source.rooms.map((room) => ({
      ...cloneRecord(room),
      device_ids: [...(room.device_ids ?? [])],
    })) as RoomInput[],
    devices: source.devices.map((device) => cloneRecord(device)),
    pipes: source.pipes.map((pipe) => cloneRecord(pipe)),
    connections: source.connections.map((connection) => cloneRecord(connection)),
    design_options: cloneRecord(source.design_options ?? {}),
  };
}

function cloneRecord<T extends object>(source: T): T {
  const result: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(source)) result[key] = cloneValue(value);
  return result as T;
}

function cloneValue(value: unknown): unknown {
  if (Array.isArray(value)) return value.map((item) => cloneValue(item));
  if (typeof value === "object" && value !== null) return cloneRecord(value);
  return value;
}

function nextId(prefix: string, taken: Set<string>): string {
  for (let n = 1; ; n += 1) {
    const candidate = `${prefix}${n}`;
    if (!taken.has(candidate)) return candidate;
  }
}

function nodeIds(project: ProjectData): Set<string> {
  return new Set([
    ...project.devices.map((device) => device.id).filter(Boolean),
    ...project.pipes.map((pipe) => pipe.id).filter(Boolean),
  ]);
}

export function editorNodeOptions(project: ProjectData): EditorNodeOption[] {
  const devices = project.devices
    .filter((device) => text(device.id))
    .map((device) => ({
      id: device.id,
      label: `${device.name || device.type || "设备"} · ${device.id}`,
      kind: "device" as const,
    }));
  const pipes = project.pipes
    .filter((pipe) => text(pipe.id))
    .map((pipe) => ({
      id: pipe.id,
      label: `${SYSTEM_LABELS[pipe.system] || pipe.system} · ${pipe.id}`,
      kind: "pipe" as const,
      system: pipe.system,
    }));
  return [...devices, ...pipes];
}

export function addDevice(project: ProjectData): { project: ProjectData; deviceId: string } {
  const taken = nodeIds(project);
  const id = nextId("device_", taken);
  const device: DeviceInput = {
    id,
    type: "风机盘管",
    name: "新设备",
    quantity: 1,
  };
  return { project: { ...project, devices: [...project.devices, device] }, deviceId: id };
}

export function updateDevice(
  project: ProjectData,
  deviceId: string,
  patch: Partial<DeviceInput>,
): ProjectData {
  return {
    ...project,
    devices: project.devices.map((device) =>
      device.id === deviceId ? { ...device, ...patch } : device,
    ),
  };
}

/** Update one row by position so duplicate IDs remain individually editable
 * while the validation panel explains the duplicate to the learner. */
export function updateDeviceAt(
  project: ProjectData,
  index: number,
  patch: Partial<DeviceInput>,
): ProjectData {
  return {
    ...project,
    devices: project.devices.map((device, current) =>
      current === index ? { ...device, ...patch } : device,
    ),
  };
}

/** Remove a device and all structural references to it. */
export function removeDevice(project: ProjectData, deviceId: string): ProjectData {
  return {
    ...project,
    devices: project.devices.filter((device) => device.id !== deviceId),
    rooms: project.rooms.map((room) => ({
      ...room,
      device_ids: (room.device_ids ?? []).filter((id) => id !== deviceId),
    })),
    connections: project.connections.filter(
      (connection) => connection.from_id !== deviceId && connection.to_id !== deviceId,
    ),
  };
}

export function removeDeviceAt(project: ProjectData, index: number): ProjectData {
  const deviceId = project.devices[index]?.id;
  if (deviceId === undefined) return project;
  const devices = project.devices.filter((_, current) => current !== index);
  // Keep references while another row with the same (currently invalid) ID
  // remains. This lets learners delete one duplicate row without silently
  // deleting the surviving row's room bindings and connections as well.
  if (devices.some((device) => device.id === deviceId)) {
    return { ...project, devices };
  }
  return removeDevice({ ...project, devices }, deviceId);
}

export function addPipe(project: ProjectData): { project: ProjectData; pipeId: string } {
  const taken = nodeIds(project);
  const id = nextId("pipe_", taken);
  const pipe: PipeInput = {
    id,
    system: "chilled_water",
    pipe_type: "unknown",
    length_m: 10,
    diameter_mm: 50,
    local_resistance_coefficient: 2,
  };
  return { project: { ...project, pipes: [...project.pipes, pipe] }, pipeId: id };
}

export function updatePipe(
  project: ProjectData,
  pipeId: string,
  patch: Partial<PipeInput>,
): ProjectData {
  return {
    ...project,
    pipes: project.pipes.map((pipe) =>
      pipe.id === pipeId ? { ...pipe, ...patch } : pipe,
    ),
  };
}

export function updatePipeAt(
  project: ProjectData,
  index: number,
  patch: Partial<PipeInput>,
): ProjectData {
  return {
    ...project,
    pipes: project.pipes.map((pipe, current) =>
      current === index ? { ...pipe, ...patch } : pipe,
    ),
  };
}

/** Remove a pipe and the connections that terminate at it. */
export function removePipe(project: ProjectData, pipeId: string): ProjectData {
  return {
    ...project,
    pipes: project.pipes.filter((pipe) => pipe.id !== pipeId),
    connections: project.connections.filter(
      (connection) => connection.from_id !== pipeId && connection.to_id !== pipeId,
    ),
  };
}

export function removePipeAt(project: ProjectData, index: number): ProjectData {
  const pipeId = project.pipes[index]?.id;
  if (pipeId === undefined) return project;
  const pipes = project.pipes.filter((_, current) => current !== index);
  // As with duplicate devices, retain connections until the final duplicate
  // pipe row is removed; otherwise a row-level delete loses valid draft data.
  if (pipes.some((pipe) => pipe.id === pipeId)) {
    return { ...project, pipes };
  }
  return removePipe({ ...project, pipes }, pipeId);
}

export function addConnection(project: ProjectData): {
  project: ProjectData;
  connectionIndex: number;
} {
  const options = editorNodeOptions(project);
  const firstDevice = options.find((option) => option.kind === "device");
  const firstPipe = options.find((option) => option.kind === "pipe");
  const first = firstDevice ?? firstPipe;
  const second = firstPipe && firstPipe.id !== first?.id
    ? firstPipe
    : options.find((option) => option.id !== first?.id);
  const connection: ConnectionInput = {
    from_id: first?.id ?? "",
    to_id: second?.id ?? "",
    system: firstPipe?.system || "chilled_water",
  };
  return {
    project: { ...project, connections: [...project.connections, connection] },
    connectionIndex: project.connections.length,
  };
}

export function updateConnection(
  project: ProjectData,
  index: number,
  patch: Partial<ConnectionInput>,
): ProjectData {
  return {
    ...project,
    connections: project.connections.map((connection, current) =>
      current === index ? { ...connection, ...patch } : connection,
    ),
  };
}

export function removeConnection(project: ProjectData, index: number): ProjectData {
  return {
    ...project,
    connections: project.connections.filter((_, current) => current !== index),
  };
}

function text(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

function finiteNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

function pushIssue(
  issues: ProjectDraftIssue[],
  code: string,
  message: string,
  target: string | null,
  field: string,
  entity: ProjectDraftIssue["entity"],
) {
  issues.push({ level: "error", code, message, target, field, entity });
}

function checkUniqueIds(
  issues: ProjectDraftIssue[],
  items: Array<{ id?: string | null }>,
  label: string,
  entity: ProjectDraftIssue["entity"],
  required: boolean,
) {
  const seen = new Map<string, number>();
  items.forEach((item, index) => {
    const id = text(item.id);
    if (!id) {
      if (required || item.id !== undefined && item.id !== null) {
        pushIssue(issues, "EDITOR_EMPTY_ID", `${label}的 ID 不能为空。`, `${label} ${index + 1}`, `${entity}s[${index}].id`, entity);
      }
      return;
    }
    const previous = seen.get(id);
    if (previous !== undefined) {
      pushIssue(issues, "EDITOR_DUPLICATE_ID", `${label} ID「${id}」重复。`, id, `${entity}s[${index}].id`, entity);
    } else seen.set(id, index);
  });
}

function checkOptionalNonNegative(
  issues: ProjectDraftIssue[],
  value: unknown,
  label: string,
  target: string,
  field: string,
  entity: ProjectDraftIssue["entity"],
) {
  if (value === null || value === undefined || value === "") return;
  if (!finiteNumber(value) || value < 0) {
    pushIssue(issues, "EDITOR_INVALID_DIMENSION", `${label}必须是大于或等于 0 的有限数值。`, target, field, entity);
  }
}

function checkOptionalPositive(
  issues: ProjectDraftIssue[],
  value: unknown,
  label: string,
  target: string,
  field: string,
  entity: ProjectDraftIssue["entity"],
) {
  if (value === null || value === undefined || value === "") return;
  if (!finiteNumber(value) || value <= 0) {
    pushIssue(issues, "EDITOR_INVALID_DIMENSION", `${label}必须是大于 0 的有限数值。`, target, field, entity);
  }
}

export function validateProjectDraft(project: ProjectData): ProjectDraftValidation {
  const issues: ProjectDraftIssue[] = [];

  checkUniqueIds(issues, project.floors, "楼层", "floor", true);
  checkUniqueIds(issues, project.rooms, "房间", "room", false);
  checkUniqueIds(issues, project.devices, "设备", "device", true);
  checkUniqueIds(issues, project.pipes, "管路", "pipe", true);

  const nodeIdOwners = new Map<string, string>();
  for (const device of project.devices) {
    const id = text(device.id);
    if (!id) continue;
    const owner = nodeIdOwners.get(id);
    if (owner) pushIssue(issues, "EDITOR_DUPLICATE_NODE_ID", `设备与${owner}不能使用相同的节点 ID「${id}」。`, id, "devices", "device");
    else nodeIdOwners.set(id, "设备");
  }
  for (const pipe of project.pipes) {
    const id = text(pipe.id);
    if (!id) continue;
    const owner = nodeIdOwners.get(id);
    if (owner) pushIssue(issues, "EDITOR_DUPLICATE_NODE_ID", `管路与${owner}不能使用相同的节点 ID「${id}」。`, id, "pipes", "pipe");
    else nodeIdOwners.set(id, "管路");
  }

  const floorIds = new Set(project.floors.map((floor) => text(floor.id)).filter(Boolean));
  const devicesById = new Map(
    project.devices
      .map((device) => [text(device.id), device] as const)
      .filter(([id]) => Boolean(id)),
  );
  const deviceIds = new Set(devicesById.keys());
  const validNodeIds = new Set(nodeIdOwners.keys());

  project.floors.forEach((floor, index) => {
    if (!finiteNumber(floor.level) || !Number.isInteger(floor.level)) pushIssue(issues, "EDITOR_INVALID_DIMENSION", "楼层层级必须是有限整数。", floor.id || `楼层 ${index + 1}`, `floors[${index}].level`, "floor");
    if (!finiteNumber(floor.height_m) || floor.height_m <= 0) pushIssue(issues, "EDITOR_INVALID_DIMENSION", "楼层高度必须大于 0。", floor.id || `楼层 ${index + 1}`, `floors[${index}].height_m`, "floor");
    if (!finiteNumber(floor.elevation_m)) pushIssue(issues, "EDITOR_INVALID_DIMENSION", "楼层标高必须是有限数值。", floor.id || `楼层 ${index + 1}`, `floors[${index}].elevation_m`, "floor");
  });

  project.rooms.forEach((room, index) => {
    const target = text(room.id) || room.name || `房间 ${index + 1}`;
    if (!text(room.name)) pushIssue(issues, "EDITOR_EMPTY_NAME", "房间名称不能为空。", target, `rooms[${index}].name`, "room");
    if (!finiteNumber(room.area_m2) || room.area_m2 <= 0) pushIssue(issues, "EDITOR_INVALID_DIMENSION", "房间面积必须大于 0。", target, `rooms[${index}].area_m2`, "room");
    if (!finiteNumber(room.people) || room.people < 0 || !Number.isInteger(room.people)) pushIssue(issues, "EDITOR_INVALID_DIMENSION", "房间人数必须是大于或等于 0 的整数。", target, `rooms[${index}].people`, "room");
    if (room.floor_id && !floorIds.has(text(room.floor_id))) pushIssue(issues, "EDITOR_UNKNOWN_FLOOR", `房间所属楼层「${room.floor_id}」不存在。`, target, `rooms[${index}].floor_id`, "room");
    const bindings = room.device_ids ?? [];
    const bindingSeen = new Set<string>();
    bindings.forEach((deviceId, bindingIndex) => {
      const value = text(deviceId);
      if (!value || !deviceIds.has(value)) pushIssue(issues, "EDITOR_UNKNOWN_DEVICE_BINDING", `房间绑定的设备「${deviceId || "（空）"}」不存在。`, target, `rooms[${index}].device_ids[${bindingIndex}]`, "room");
      else if (!isRoomTerminalDevice(devicesById.get(value)!)) pushIssue(issues, "EDITOR_NON_TERMINAL_BINDING", `设备「${value}」不是可服务房间的末端设备。`, target, `rooms[${index}].device_ids[${bindingIndex}]`, "room");
      if (bindingSeen.has(value)) pushIssue(issues, "EDITOR_DUPLICATE_BINDING", `房间重复绑定设备「${value}」。`, target, `rooms[${index}].device_ids[${bindingIndex}]`, "room");
      bindingSeen.add(value);
    });
  });

  project.devices.forEach((device, index) => {
    const target = text(device.id) || `设备 ${index + 1}`;
    if (!text(device.type)) pushIssue(issues, "EDITOR_EMPTY_TYPE", "设备类型不能为空。", target, `devices[${index}].type`, "device");
    if (!finiteNumber(device.quantity) || device.quantity < 1 || !Number.isInteger(device.quantity)) pushIssue(issues, "EDITOR_INVALID_DIMENSION", "设备数量必须是大于或等于 1 的整数。", target, `devices[${index}].quantity`, "device");
    checkOptionalNonNegative(issues, device.capacity_kw, "设备容量", target, `devices[${index}].capacity_kw`, "device");
    checkOptionalNonNegative(issues, device.flow_m3_h, "设备流量", target, `devices[${index}].flow_m3_h`, "device");
    checkOptionalNonNegative(issues, device.head_m, "设备扬程", target, `devices[${index}].head_m`, "device");
    checkOptionalNonNegative(issues, device.power_kw, "设备功率", target, `devices[${index}].power_kw`, "device");
    checkOptionalNonNegative(issues, device.pressure_drop_pa, "设备压降", target, `devices[${index}].pressure_drop_pa`, "device");
    if (device.floor_id && !floorIds.has(text(device.floor_id))) pushIssue(issues, "EDITOR_UNKNOWN_FLOOR", `设备所属楼层「${device.floor_id}」不存在。`, target, `devices[${index}].floor_id`, "device");
  });

  project.pipes.forEach((pipe, index) => {
    const target = text(pipe.id) || `管路 ${index + 1}`;
    if (!HVAC_SYSTEMS.includes(pipe.system as HvacSystem)) pushIssue(issues, "EDITOR_UNSUPPORTED_SYSTEM", `管路系统「${pipe.system || "（空）"}」不受支持。`, target, `pipes[${index}].system`, "pipe");
    if (!PIPE_TYPES.includes((pipe.pipe_type || "unknown") as PipeType)) pushIssue(issues, "EDITOR_UNSUPPORTED_PIPE_TYPE", `管路类型「${pipe.pipe_type || "（空）"}」不受支持。`, target, `pipes[${index}].pipe_type`, "pipe");
    if (!finiteNumber(pipe.length_m) || pipe.length_m < 0) pushIssue(issues, "EDITOR_INVALID_DIMENSION", "管路长度必须是大于或等于 0 的有限数值。", target, `pipes[${index}].length_m`, "pipe");
    checkOptionalPositive(issues, pipe.diameter_mm, "管径", target, `pipes[${index}].diameter_mm`, "pipe");
    checkOptionalPositive(issues, pipe.width_mm, "风管宽度", target, `pipes[${index}].width_mm`, "pipe");
    checkOptionalPositive(issues, pipe.height_mm, "风管高度", target, `pipes[${index}].height_mm`, "pipe");
    checkOptionalPositive(issues, pipe.flow_m3_h, "管路流量", target, `pipes[${index}].flow_m3_h`, "pipe");
    checkOptionalNonNegative(issues, pipe.local_resistance_coefficient, "局部阻力系数", target, `pipes[${index}].local_resistance_coefficient`, "pipe");
    if (pipe.floor_id && !floorIds.has(text(pipe.floor_id))) pushIssue(issues, "EDITOR_UNKNOWN_FLOOR", `管路所属楼层「${pipe.floor_id}」不存在。`, target, `pipes[${index}].floor_id`, "pipe");
  });

  const connectionSeen = new Set<string>();
  project.connections.forEach((connection, index) => {
    const from = text(connection.from_id);
    const to = text(connection.to_id);
    const target = `${from || "（空）"} → ${to || "（空）"}`;
    if (!HVAC_SYSTEMS.includes(connection.system as HvacSystem)) pushIssue(issues, "EDITOR_UNSUPPORTED_SYSTEM", `连接系统「${connection.system || "（空）"}」不受支持。`, target, `connections[${index}].system`, "connection");
    if (!from || !validNodeIds.has(from)) pushIssue(issues, "EDITOR_UNKNOWN_CONNECTION_NODE", `连接起点「${from || "（空）"}」不是已知设备或管路。`, target, `connections[${index}].from_id`, "connection");
    if (!to || !validNodeIds.has(to)) pushIssue(issues, "EDITOR_UNKNOWN_CONNECTION_NODE", `连接终点「${to || "（空）"}」不是已知设备或管路。`, target, `connections[${index}].to_id`, "connection");
    if (from && from === to) pushIssue(issues, "EDITOR_SELF_CONNECTION", "连接的起点和终点不能相同。", target, `connections[${index}]`, "connection");
    const key = `${connection.system}|${from}|${to}`;
    if (connectionSeen.has(key)) pushIssue(issues, "EDITOR_DUPLICATE_CONNECTION", "连接关系重复。", target, `connections[${index}]`, "connection");
    connectionSeen.add(key);
    const fromPipe = project.pipes.find((pipe) => pipe.id === from);
    const toPipe = project.pipes.find((pipe) => pipe.id === to);
    for (const pipe of [fromPipe, toPipe]) {
      if (pipe && pipe.system !== connection.system) pushIssue(issues, "EDITOR_SYSTEM_MISMATCH", `连接系统与管路「${pipe.id}」的系统不一致。`, target, `connections[${index}].system`, "connection");
    }
  });

  const unique = new Map<string, ProjectDraftIssue>();
  for (const issue of issues) unique.set(`${issue.code}|${issue.field}|${issue.target}`, issue);
  return { valid: unique.size === 0, issues: [...unique.values()] };
}
