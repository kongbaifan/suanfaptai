import type { FloorInput, ProjectData, RoomInput } from "@/types";

// The editor helpers live in their own module so the floor/room tree remains
// easy to reason about. Re-exporting them here keeps the existing building
// utility import path stable for callers and tests.
export {
  DEVICE_TYPE_OPTIONS,
  DEVICE_LOCATION_OPTIONS,
  HVAC_SYSTEMS,
  PIPE_TYPES,
  PIPE_TYPE_LABELS,
  ROOM_PURPOSE_OPTIONS,
  ROOM_TERMINAL_DEVICE_TYPES,
  SYSTEM_LABELS,
  addConnection,
  addDevice,
  addPipe,
  cloneProjectData,
  editorNodeOptions,
  deviceLocationLabel,
  isKnownDeviceLocation,
  isRoomTerminalDevice,
  removeConnection,
  removeDevice,
  removeDeviceAt,
  removePipe,
  removePipeAt,
  roomPurposeLabel,
  updateConnection,
  updateDevice,
  updateDeviceAt,
  updatePipe,
  updatePipeAt,
  terminalDevices,
  validateProjectDraft,
} from "./project-editor";
export type {
  EditorNodeOption,
  HvacSystem,
  PipeType,
  ProjectDraftIssue,
  ProjectDraftValidation,
  RoomPurpose,
} from "./project-editor";

/** 楼层与房间的树操作。全是纯函数：接收 ProjectData 返回新的 ProjectData，
 *  不改入参，便于在视图里做「草稿 -> 保存」而不污染 store 里的已存副本。
 *
 *  绑定方向是房间持有 device_ids，所以删房间/删楼层不再需要跨设备级联清理：
 *  清单跟着房间一起消失。反方向（删设备）才需要摘除引用，见 stripDeviceBindings。 */

export interface KeyedRoom {
  key: string;
  room: RoomInput;
}

export interface FloorNode {
  floor: FloorInput;
  rooms: KeyedRoom[];
}

/** 房间的有效 ID。后端 room_instance_id 对无 id 的房间按下标生成 `room-N`，
 *  树里新建的房间一律显式给 id，避免重排后标识跟着位置变。 */
export function roomKey(room: RoomInput, index: number): string {
  return room.id || `room-${index + 1}`;
}

/** 按楼层分组。楼层按 level 升序，与后端 normalized_floors 一致；
 *  floor_id 对不上任何楼层的房间归到 orphans，界面需要显式暴露而不是静默丢弃。 */
export function buildTree(project: ProjectData): { nodes: FloorNode[]; orphans: KeyedRoom[] } {
  const floors = [...project.floors].sort(
    (a, b) => a.level - b.level || a.elevation_m - b.elevation_m || a.id.localeCompare(b.id),
  );
  const known = new Set(floors.map((floor) => floor.id));
  // key 在这里一次算好：下标必须取自原数组，交给模板反查会既慢又易错位。
  const keyed = project.rooms.map((room, index) => ({ key: roomKey(room, index), room }));
  const nodes = floors.map((floor) => ({
    floor,
    rooms: keyed.filter((item) => item.room.floor_id === floor.id),
  }));
  const orphans = keyed.filter(
    (item) => !item.room.floor_id || !known.has(item.room.floor_id),
  );
  return { nodes, orphans };
}

function nextId(prefix: string, taken: Set<string>): string {
  for (let n = 1; ; n += 1) {
    const candidate = `${prefix}${n}`;
    if (!taken.has(candidate)) return candidate;
  }
}

export function addFloor(project: ProjectData): { project: ProjectData; floorId: string } {
  const taken = new Set(project.floors.map((floor) => floor.id));
  const id = nextId("F", taken);
  const maxLevel = project.floors.reduce((max, floor) => Math.max(max, floor.level), 0);
  const template = project.floors.find((floor) => floor.level === maxLevel);
  const height = template?.height_m ?? 3.6;
  const floor: FloorInput = {
    id,
    name: `${maxLevel + 1} 层`,
    level: maxLevel + 1,
    // 叠在最高层之上：标高取其楼面标高加层高，避免两层同标高。
    elevation_m: (template?.elevation_m ?? 0) + height,
    height_m: height,
  };
  return { project: { ...project, floors: [...project.floors, floor] }, floorId: id };
}

/** 删楼层连带删该层房间。房间自己持有 device_ids，无需再跨设备清理。 */
export function removeFloor(project: ProjectData, floorId: string): ProjectData {
  return {
    ...project,
    floors: project.floors.filter((floor) => floor.id !== floorId),
    rooms: project.rooms.filter((room) => room.floor_id !== floorId),
  };
}

export function renameFloor(project: ProjectData, floorId: string, name: string): ProjectData {
  return {
    ...project,
    floors: project.floors.map((floor) => (floor.id === floorId ? { ...floor, name } : floor)),
  };
}

export function addRoom(project: ProjectData, floorId: string): { project: ProjectData; roomId: string } {
  const taken = new Set(
    project.rooms.map((room, index) => roomKey(room, index)),
  );
  const id = nextId("room_", taken);
  // area_m2 后端是 Field(gt=0)，不能给 0；其余字段留给后端默认值。
  // device_ids 起手为空：后端只报 ROOM_BINDING_NO_DEVICE 警告，不阻塞保存。
  const room: RoomInput = { id, floor_id: floorId, name: "新房间", device_ids: [], area_m2: 100, people: 0 };
  return { project: { ...project, rooms: [...project.rooms, room] }, roomId: id };
}

/** 删房间。device_ids 随房间一起消失，不需要动 devices。 */
export function removeRoom(project: ProjectData, roomId: string): ProjectData {
  return {
    ...project,
    rooms: project.rooms.filter((room, index) => roomKey(room, index) !== roomId),
  };
}

export function updateRoom(project: ProjectData, roomId: string, patch: Partial<RoomInput>): ProjectData {
  return {
    ...project,
    rooms: project.rooms.map((room, index) =>
      roomKey(room, index) === roomId ? { ...room, ...patch } : room,
    ),
  };
}

/** 设置某房间的末端设备清单。 */
export function setRoomDevices(project: ProjectData, roomId: string, deviceIds: string[]): ProjectData {
  // 去重后写入：后端对重复项报 ROOM_BINDING_DUPLICATE（error）。
  return updateRoom(project, roomId, { device_ids: [...new Set(deviceIds)] });
}

/** 删设备时从所有房间的 device_ids 里摘除，避免落盘时撞 ROOM_BINDING_UNKNOWN。
 *  这是级联唯一还需要的方向——设备被删，引用它的房间才要改。 */
export function stripDeviceBindings(project: ProjectData, deviceIds: string[]): ProjectData {
  const doomed = new Set(deviceIds);
  return {
    ...project,
    rooms: project.rooms.map((room) => {
      const bindings = room.device_ids ?? [];
      const kept = bindings.filter((id) => !doomed.has(id));
      return kept.length === bindings.length ? room : { ...room, device_ids: kept };
    }),
  };
}

/** 哪些房间会因为删这些设备而失去全部末端，供确认提示使用。 */
export function roomsLosingAllDevices(project: ProjectData, deviceIds: string[]): string[] {
  const doomed = new Set(deviceIds);
  return project.rooms
    .map((room, index) => ({ key: roomKey(room, index), devices: room.device_ids ?? [] }))
    .filter((item) => item.devices.length > 0 && item.devices.every((id) => doomed.has(id)))
    .map((item) => item.key);
}

/** 删楼层时会一并消失的房间 ID，供确认提示使用。 */
export function roomIdsOnFloor(project: ProjectData, floorId: string): string[] {
  return project.rooms
    .map((room, index) => ({ key: roomKey(room, index), floorId: room.floor_id }))
    .filter((item) => item.floorId === floorId)
    .map((item) => item.key);
}
