import { describe, expect, it } from "vitest";
import {
  DEVICE_TYPE_OPTIONS,
  ROOM_TERMINAL_DEVICE_TYPES,
  addConnection,
  addDevice,
  addPipe,
  canonicalDeviceType,
  cloneProjectData,
  deviceLocationLabel,
  editorNodeOptions,
  isKnownDeviceLocation,
  isRoomTerminalDevice,
  removeDevice,
  removeDeviceAt,
  removePipe,
  removePipeAt,
  terminalDevices,
  updateConnection,
  validateProjectDraft,
} from "./project-editor";
import type { ProjectData } from "@/types";

function project(overrides: Partial<ProjectData> = {}): ProjectData {
  return {
    floors: [{ id: "F1", name: "一层", level: 1, elevation_m: 0, height_m: 3.6 }],
    rooms: [{ id: "r1", floor_id: "F1", name: "教室", device_ids: [], area_m2: 50, people: 10 }],
    devices: [{ id: "fcu1", type: "风机盘管", name: "FCU", quantity: 1 }],
    pipes: [{ id: "pipe1", system: "chilled_water", pipe_type: "branch_supply", length_m: 10, diameter_mm: 40 }],
    connections: [{ from_id: "fcu1", to_id: "pipe1", system: "chilled_water" }],
    simultaneity_factor: 0.9,
    safety_factor: 1.1,
    design_options: { nested: { enabled: true } },
    ...overrides,
  };
}

describe("project editor helpers", () => {
  it("copies nested draft data without sharing arrays or objects", () => {
    const source = project();
    (source.floors[0] as typeof source.floors[number] & { metadata: { color: string } }).metadata = { color: "blue" };
    (source.devices[0] as typeof source.devices[number] & { metadata: { aliases: string[] } }).metadata = { aliases: ["A"] };
    (source.pipes[0] as typeof source.pipes[number] & { metadata: { tags: string[] } }).metadata = { tags: ["supply"] };
    (source.connections[0] as typeof source.connections[number] & { metadata: { label: string } }).metadata = { label: "main" };
    const copy = cloneProjectData(source);
    copy.rooms[0].device_ids.push("fcu1");
    (copy.design_options.nested as { enabled: boolean }).enabled = false;
    (copy.floors[0] as typeof copy.floors[number] & { metadata: { color: string } }).metadata.color = "red";
    (copy.devices[0] as typeof copy.devices[number] & { metadata: { aliases: string[] } }).metadata.aliases.push("B");
    (copy.pipes[0] as typeof copy.pipes[number] & { metadata: { tags: string[] } }).metadata.tags.push("return");
    (copy.connections[0] as typeof copy.connections[number] & { metadata: { label: string } }).metadata.label = "branch";

    expect(source.rooms[0].device_ids).toEqual([]);
    expect((source.design_options.nested as { enabled: boolean }).enabled).toBe(true);
    expect((source.floors[0] as typeof source.floors[number] & { metadata: { color: string } }).metadata.color).toBe("blue");
    expect((source.devices[0] as typeof source.devices[number] & { metadata: { aliases: string[] } }).metadata.aliases).toEqual(["A"]);
    expect((source.pipes[0] as typeof source.pipes[number] & { metadata: { tags: string[] } }).metadata.tags).toEqual(["supply"]);
    expect((source.connections[0] as typeof source.connections[number] & { metadata: { label: string } }).metadata.label).toBe("main");
  });

  it("adds structural rows with canonical minimum fields", () => {
    const source = project();
    const withDevice = addDevice(source).project;
    const withPipe = addPipe(withDevice).project;
    const withConnection = addConnection(withPipe).project;

    expect(withDevice.devices.at(-1)).toMatchObject({ id: "device_1", type: "风机盘管", quantity: 1 });
    expect(withPipe.pipes.at(-1)).toMatchObject({ id: "pipe_1", system: "chilled_water", length_m: 10 });
    expect(withConnection.connections.at(-1)).toMatchObject({ from_id: "fcu1", to_id: "pipe1" });
    expect(editorNodeOptions(withPipe).map((node) => node.id)).toEqual(["fcu1", "device_1", "pipe1", "pipe_1"]);
  });

  it("flags empty/duplicate IDs, unknown nodes, unsupported systems, and dimensions", () => {
    const result = validateProjectDraft(project({
      devices: [
        { id: "fcu1", type: "风机盘管", quantity: 1 },
        { id: "fcu1", type: "", quantity: 0 },
      ],
      pipes: [{ id: "pipe1", system: "other", length_m: -1, diameter_mm: 0 }],
      connections: [{ from_id: "missing", to_id: "pipe1", system: "other" }],
    }));

    const codes = result.issues.map((issue) => issue.code);
    expect(result.valid).toBe(false);
    expect(codes).toContain("EDITOR_DUPLICATE_ID");
    expect(codes).toContain("EDITOR_UNSUPPORTED_SYSTEM");
    expect(codes).toContain("EDITOR_UNKNOWN_CONNECTION_NODE");
    expect(codes).toContain("EDITOR_INVALID_DIMENSION");
  });

  it("removes device/pipe references from rooms and connections", () => {
    const source = project({ rooms: [{ id: "r1", floor_id: "F1", name: "教室", device_ids: ["fcu1"], area_m2: 50, people: 10 }] });
    const withoutDevice = removeDevice(source, "fcu1");
    expect(withoutDevice.rooms[0].device_ids).toEqual([]);
    expect(withoutDevice.connections).toEqual([]);

    const withoutPipe = removePipe(source, "pipe1");
    expect(withoutPipe.devices).toHaveLength(1);
    expect(withoutPipe.connections).toEqual([]);
  });

  it("deletes only the selected duplicate row and keeps surviving references", () => {
    const source = project({
      devices: [
        { id: "dup", type: "风机盘管", quantity: 1 },
        { id: "dup", type: "循环泵", quantity: 1 },
      ],
      rooms: [{ id: "r1", floor_id: "F1", name: "教室", device_ids: ["dup"], area_m2: 50, people: 10 }],
      connections: [{ from_id: "dup", to_id: "pipe1", system: "chilled_water" }],
    });

    const after = removeDevice(source, "dup");
    // The ID-based helper intentionally removes all matching rows; the row
    // helper is what the table uses when a duplicate is being corrected.
    expect(after.devices).toHaveLength(0);

    const rowDeleted = removeDeviceAt(source, 0);
    expect(rowDeleted.devices).toHaveLength(1);
    expect(rowDeleted.rooms[0].device_ids).toEqual(["dup"]);
    expect(rowDeleted.connections).toEqual(source.connections);

    const duplicatePipes = project({
      pipes: [
        { id: "pipe1", system: "chilled_water", length_m: 10 },
        { id: "pipe1", system: "chilled_water", length_m: 20 },
      ],
    });
    const pipeRowDeleted = removePipeAt(duplicatePipes, 0);
    expect(pipeRowDeleted.pipes).toHaveLength(1);
    expect(pipeRowDeleted.connections).toEqual(duplicatePipes.connections);
  });

  it("updates and removes a connection by stable row index", () => {
    const source = project({ connections: [
      { from_id: "fcu1", to_id: "pipe1", system: "chilled_water" },
      { from_id: "pipe1", to_id: "fcu1", system: "chilled_water" },
    ] });
    const changed = updateConnection(source, 1, { system: "air" });
    expect(changed.connections[1].system).toBe("air");
  });

  it("filters room candidates to the canonical terminal types only", () => {
    const source = project({
      devices: [
        { id: "fcu", type: "风机盘管", quantity: 1 },
        { id: "ahu", type: "组合式空调机组", quantity: 1 },
        { id: "fau", type: "新风机组", quantity: 1 },
        { id: "chiller", type: "冷源机组", quantity: 1 },
        { id: "pump", type: "循环泵", quantity: 1 },
        { id: "tower", type: "冷却塔", quantity: 1 },
        { id: "heat", type: "空气源热泵", quantity: 1 },
        { id: "unknown", type: "自定义设备", quantity: 1 },
      ],
    });

    expect(terminalDevices(source).map((device) => device.id)).toEqual(["fcu", "ahu", "fau"]);
    expect(isRoomTerminalDevice({ type: "FCU" })).toBe(false);
    expect(isRoomTerminalDevice({ type: "冷却塔" })).toBe(false);
  });

  it("offers air grilles as addable device types without treating them as room terminals", () => {
    // 风口必须能在「新增设备」弹窗里选到（此前缺失，用户手搭项目加不了风口）。
    expect(DEVICE_TYPE_OPTIONS).toContain("送风口");
    expect(DEVICE_TYPE_OPTIONS).toContain("回风口");

    // 但风口不是房间末端：房间「服务末端」选择器不得出现风口，
    // 否则后端会以 ROOM_BINDING_UNSUPPORTED_DEVICE 拒绝。
    expect(isRoomTerminalDevice({ type: "送风口" })).toBe(false);
    expect(isRoomTerminalDevice({ type: "回风口" })).toBe(false);
    expect(ROOM_TERMINAL_DEVICE_TYPES).not.toContain("送风口");
    expect(ROOM_TERMINAL_DEVICE_TYPES).not.toContain("回风口");
  });

  it("accepts legacy device type names that the backend still aliases", () => {
    // 后端 app/data/equipment.py 的 LEGACY_DEVICE_TYPE_ALIASES 把历史类型名
    // 「一体式新风机组」归一为「新风机组」。此前保存的项目（含内置样例与
    // 「中央空调示例项目」）仍带旧名，后端能算，前端只认现名就会报出
    // 「设备「fau_f1」不是可服务房间的末端设备」这个**前端自造的假错误**。
    expect(canonicalDeviceType("一体式新风机组")).toBe("新风机组");
    expect(isRoomTerminalDevice({ type: "一体式新风机组" })).toBe(true);
    expect(isRoomTerminalDevice({ type: " 一体式新风机组 " })).toBe(true);

    // 未知类型仍必须被拒（不能为了兼容把判据放宽成"什么都收"）。
    expect(canonicalDeviceType("自定义设备")).toBe("自定义设备");
    expect(isRoomTerminalDevice({ type: "自定义设备" })).toBe(false);
  });

  it("aliases the chiller synonym exactly like the backend does", () => {
    // 「冷水机组」是后端权威表里的同义词（映射到目录现名「冷源机组」）。
    // 前端漏掉它时，实时页在 rated 尚未加载（role 为空）的那段时间里
    // cardRole 拿不到 cold_source，冷机卡片会整块消失。
    expect(canonicalDeviceType("冷水机组")).toBe("冷源机组");
    expect(canonicalDeviceType(" 冷水机组 ")).toBe("冷源机组");
    // 冷源不是房间末端，归一后仍必须被拒。
    expect(isRoomTerminalDevice({ type: "冷水机组" })).toBe(false);
  });

  it("treats Object.prototype member names as unknown types instead of throwing", () => {
    // 别名表若用对象字面量实现，`aliases["constructor"]` 会命中原型上的函数，
    // `?? 兜底` 不生效，返回的不是字符串，调用方 `.toLocaleLowerCase()` 直接抛错
    // ——整份草稿校验崩掉。设备类型是自由文本，这些值必须走"未知类型"分支。
    for (const key of ["constructor", "__proto__", "toString", "valueOf", "hasOwnProperty"]) {
      expect(canonicalDeviceType(key)).toBe(key);
      expect(() => isRoomTerminalDevice({ type: key })).not.toThrow();
      expect(isRoomTerminalDevice({ type: key })).toBe(false);
    }
  });

  it("does not flag a legacy-typed room terminal as a non-terminal binding", () => {
    const source = project({
      rooms: [{ id: "r1", floor_id: "F1", name: "教室", device_ids: ["fau"], area_m2: 50, people: 10 }],
      devices: [{ id: "fau", type: "一体式新风机组", name: "BYKJD12", quantity: 1 }],
    });

    const result = validateProjectDraft(source);
    expect(result.issues.some((issue) => issue.code === "EDITOR_NON_TERMINAL_BINDING")).toBe(false);
  });

  it("labels known placement values and preserves unknown values", () => {
    expect(deviceLocationLabel("equipment_room")).toBe("机房");
    expect(deviceLocationLabel("roof")).toBe("屋顶");
    expect(deviceLocationLabel("lab mezzanine")).toBe("其他：lab mezzanine");
    expect(isKnownDeviceLocation("equipment_room")).toBe(true);
    expect(isKnownDeviceLocation("lab mezzanine")).toBe(false);

    const source = project({ devices: [{ id: "pump", type: "循环泵", quantity: 1, floor_id: "F1", location_type: "lab mezzanine" }] });
    const copy = cloneProjectData(source);
    expect(copy.devices[0]).toMatchObject({ floor_id: "F1", location_type: "lab mezzanine" });
  });
});
