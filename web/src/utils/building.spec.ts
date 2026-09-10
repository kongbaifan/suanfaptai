import { describe, expect, it } from "vitest";
import {
  addFloor,
  addRoom,
  buildTree,
  removeFloor,
  removeRoom,
  renameFloor,
  roomIdsOnFloor,
  roomsLosingAllDevices,
  setRoomDevices,
  stripDeviceBindings,
  updateRoom,
} from "./building";
import type { DeviceInput, ProjectData } from "@/types";

function ahu(id = "ahu_f1"): DeviceInput {
  return { id, type: "组合式空调机组", quantity: 1 };
}

function project(overrides: Partial<ProjectData> = {}): ProjectData {
  return {
    floors: [
      { id: "F1", name: "一层", level: 1, elevation_m: 0, height_m: 3.6 },
      { id: "F2", name: "二层", level: 2, elevation_m: 3.6, height_m: 3.6 },
    ],
    rooms: [
      { id: "r_office", floor_id: "F1", name: "办公区", device_ids: [], area_m2: 180, people: 30 },
      { id: "r_meeting", floor_id: "F1", name: "会议室", device_ids: [], area_m2: 80, people: 24 },
      { id: "r_training", floor_id: "F2", name: "培训室", device_ids: [], area_m2: 110, people: 32 },
    ],
    devices: [],
    pipes: [],
    connections: [],
    simultaneity_factor: 0.9,
    safety_factor: 1.1,
    design_options: {},
    ...overrides,
  };
}

describe("buildTree", () => {
  it("groups rooms under floors ordered by level", () => {
    const { nodes, orphans } = buildTree(project());

    expect(nodes.map((node) => node.floor.id)).toEqual(["F1", "F2"]);
    expect(nodes[0].rooms.map((item) => item.key)).toEqual(["r_office", "r_meeting"]);
    expect(nodes[1].rooms.map((item) => item.key)).toEqual(["r_training"]);
    expect(orphans).toEqual([]);
  });

  it("keys rooms by their original index so id-less rooms stay addressable", () => {
    const source = project({
      rooms: [
        { floor_id: "F1", name: "无 id 房间", device_ids: [], area_m2: 50, people: 2 },
        { id: "r_x", floor_id: "F1", name: "有 id", device_ids: [], area_m2: 60, people: 3 },
      ],
    });

    expect(buildTree(source).nodes[0].rooms.map((item) => item.key)).toEqual(["room-1", "r_x"]);
  });

  it("surfaces rooms whose floor_id matches no floor instead of dropping them", () => {
    const source = project({
      rooms: [{ id: "r_ghost", floor_id: "F9", name: "孤儿", device_ids: [], area_m2: 70, people: 1 }],
    });
    const { nodes, orphans } = buildTree(source);

    expect(nodes.every((node) => node.rooms.length === 0)).toBe(true);
    expect(orphans.map((item) => item.key)).toEqual(["r_ghost"]);
  });
});

describe("floor edits", () => {
  it("stacks a new floor above the highest one without reusing an id", () => {
    const { project: next, floorId } = addFloor(project());

    expect(floorId).toBe("F3");
    const added = next.floors.find((floor) => floor.id === "F3")!;
    expect(added.level).toBe(3);
    // 标高取最高层楼面加层高，两层不能同标高。
    expect(added.elevation_m).toBe(7.2);
    expect(project().floors).toHaveLength(2);
  });

  it("fills the first free id when ids are non-contiguous", () => {
    const source = project({
      floors: [{ id: "F2", name: "二层", level: 2, elevation_m: 3.6, height_m: 3.6 }],
    });

    expect(addFloor(source).floorId).toBe("F1");
  });

  it("deletes the floor together with its rooms", () => {
    const next = removeFloor(project(), "F1");

    expect(next.floors.map((floor) => floor.id)).toEqual(["F2"]);
    expect(next.rooms.map((room) => room.id)).toEqual(["r_training"]);
  });

  it("renames only the target floor", () => {
    const next = renameFloor(project(), "F2", "顶层");

    expect(next.floors.map((floor) => floor.name)).toEqual(["一层", "顶层"]);
  });

  it("lists the rooms a floor deletion would take with it", () => {
    expect(roomIdsOnFloor(project(), "F1")).toEqual(["r_office", "r_meeting"]);
  });
});

describe("room edits", () => {
  it("adds a room with a positive default area on the requested floor", () => {
    const { project: next, roomId } = addRoom(project(), "F2");

    expect(roomId).toBe("room_1");
    const added = next.rooms.find((room) => room.id === "room_1")!;
    expect(added.floor_id).toBe("F2");
    // 后端 area_m2 是 Field(gt=0)，默认值不能是 0。
    expect(added.area_m2).toBeGreaterThan(0);
  });

  it("patches one room and leaves its other fields intact", () => {
    const next = updateRoom(project(), "r_office", { area_m2: 250 });
    const room = next.rooms.find((item) => item.id === "r_office")!;

    expect(room.area_m2).toBe(250);
    expect(room.people).toBe(30);
    expect(next.rooms.find((item) => item.id === "r_meeting")!.area_m2).toBe(80);
  });

  it("moves a room to another floor", () => {
    const next = updateRoom(project(), "r_training", { floor_id: "F1" });

    expect(buildTree(next).nodes[0].rooms.map((item) => item.key)).toEqual([
      "r_office",
      "r_meeting",
      "r_training",
    ]);
  });
});

describe("room-owned device bindings", () => {
  function bound(): ProjectData {
    const source = project({ devices: [ahu()] });
    return setRoomDevices(setRoomDevices(source, "r_office", ["ahu_f1"]), "r_meeting", ["ahu_f1"]);
  }

  it("writes the device list onto the room, de-duplicated", () => {
    const next = setRoomDevices(project({ devices: [ahu()] }), "r_office", [
      "ahu_f1",
      "ahu_f1",
    ]);

    expect(next.rooms.find((room) => room.id === "r_office")!.device_ids).toEqual(["ahu_f1"]);
  });

  it("drops the binding along with the room, leaving devices untouched", () => {
    const next = removeRoom(bound(), "r_meeting");

    expect(next.rooms.map((room) => room.id)).toEqual(["r_office", "r_training"]);
    expect(next.devices).toEqual([ahu()]);
  });

  it("drops bindings of every room a floor deletion takes with it", () => {
    const next = removeFloor(bound(), "F1");

    expect(next.rooms.map((room) => room.id)).toEqual(["r_training"]);
    expect(next.devices).toEqual([ahu()]);
  });

  it("strips a deleted device from every room that listed it", () => {
    const next = stripDeviceBindings(bound(), ["ahu_f1"]);

    expect(next.rooms.map((room) => room.device_ids)).toEqual([[], [], []]);
  });

  it("names the rooms that would lose every terminal serving them", () => {
    const source = setRoomDevices(bound(), "r_office", ["ahu_f1", "fcu_x"]);

    expect(roomsLosingAllDevices(source, ["ahu_f1"])).toEqual(["r_meeting"]);
    expect(roomsLosingAllDevices(source, ["ahu_f1", "fcu_x"])).toEqual([
      "r_office",
      "r_meeting",
    ]);
  });
});
