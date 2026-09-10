// @vitest-environment jsdom

import { createPinia, setActivePinia } from "pinia";
import { flushPromises, mount } from "@vue/test-utils";
import { beforeEach, describe, expect, it, vi } from "vitest";
import BuildingView from "./BuildingView.vue";
import { useProjectStore } from "@/stores/project";
import type { ProjectData, ProjectRecord } from "@/types";

vi.mock("@/api/client", () => ({
  api: vi.fn(),
  errorMessage: (reason: unknown) => String(reason),
}));

import { api } from "@/api/client";

const apiMock = vi.mocked(api);

const data: ProjectData = {
  floors: [
    { id: "B1F", name: "地下一层机房", level: -1, elevation_m: -4, height_m: 4 },
    { id: "F1", name: "一层", level: 1, elevation_m: 0, height_m: 3.6 },
    { id: "RF", name: "屋顶", level: 3, elevation_m: 7.2, height_m: 3.6 },
  ],
  rooms: [{ id: "r1", floor_id: "F1", name: "教室", device_ids: ["fcu"], area_m2: 80, people: 12 }],
  devices: [
    { id: "chiller", type: "冷源机组", floor_id: "B1F", location_type: "equipment_room", quantity: 1 },
    { id: "pump", type: "循环泵", floor_id: "B1F", location_type: "equipment_room", quantity: 1 },
    { id: "tower", type: "冷却塔", floor_id: "RF", location_type: "roof", quantity: 1 },
    { id: "heat", type: "空气源热泵", floor_id: "RF", location_type: "roof", quantity: 1 },
    { id: "fcu", type: "风机盘管", floor_id: "F1", location_type: "lab mezzanine", quantity: 1 },
    { id: "ahu", type: "组合式空调机组", floor_id: "F1", quantity: 1 },
    { id: "unknown", type: "自定义设备", floor_id: "F1", quantity: 1 },
  ],
  pipes: [],
  connections: [],
  simultaneity_factor: 0.9,
  safety_factor: 1.1,
  design_options: {},
};

function record(project: ProjectData): ProjectRecord {
  return {
    project_id: "p1",
    name: "测试项目",
    description: null,
    tags: [],
    created_at: "",
    updated_at: "",
    history_count: 0,
    project,
    history: [],
  };
}

async function mountView() {
  setActivePinia(createPinia());
  const store = useProjectStore();
  store.active = record(data);
  store.usingSample = false;
  const wrapper = mount(BuildingView);
  await flushPromises();
  return { wrapper, store };
}

describe("BuildingView placement and terminal separation", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    apiMock.mockResolvedValue([]);
  });

  it("only offers canonical FCU/AHU/fresh-air terminals for room service", async () => {
    const { wrapper } = await mountView();
    // 默认选中按 level 升序的第一个楼层（B1F），先切到一层（房间所在层）
    await wrapper.findAll(".floor-nav-item")[1].trigger("click");

    const addSelect = wrapper.find("select[aria-label='教室 添加末端']");
    expect(addSelect.exists()).toBe(true);
    const options = addSelect.findAll("option").map((item) => item.text());
    // fcu 已绑定成 chip，下拉里剩 ahu 可选；冷源/泵/塔/热泵/自定义都不出现
    expect(options.some((text) => text.includes("ahu"))).toBe(true);
    expect(options.some((text) => text.includes("fcu"))).toBe(false);
    expect(options.some((text) => text.includes("chiller"))).toBe(false);
    expect(options.some((text) => text.includes("pump"))).toBe(false);
    expect(options.some((text) => text.includes("tower"))).toBe(false);
    expect(options.some((text) => text.includes("heat"))).toBe(false);
    expect(options.some((text) => text.includes("unknown"))).toBe(false);

    // 已绑定的 fcu 以 chip 形式可见
    expect(wrapper.find(".terminal-chip").text()).toContain("fcu");
  });

  it("shows and round-trips floor_id, known and unknown location_type values", async () => {
    const { wrapper, store } = await mountView();
    const save = vi.spyOn(store, "updateProject").mockResolvedValue(true);

    // B1F 是 level 最低层，默认选中；pump 就在那层。安装楼层收在详情里，先展开
    await wrapper.find("button[aria-label='pump 详情']").trigger("click");
    const pumpFloor = wrapper.find('select[aria-label="pump 所属楼层"]');
    const pumpLocation = wrapper.find('select[aria-label="pump 安装位置"]');
    expect((pumpFloor.element as HTMLSelectElement).value).toBe("B1F");
    expect((pumpLocation.element as HTMLSelectElement).value).toBe("equipment_room");

    // fcu 在一层，切过去验证未知位置值回显
    await wrapper.findAll(".floor-nav-item")[1].trigger("click");
    await wrapper.find("button[aria-label='fcu 详情']").trigger("click");
    const unknownLocation = wrapper.find('select[aria-label="fcu 安装位置"]');
    expect((unknownLocation.element as HTMLSelectElement).value).toBe("lab mezzanine");
    expect(unknownLocation.text()).toContain("其他：lab mezzanine");

    await pumpFloor.setValue("RF");
    await pumpLocation.setValue("roof");
    await unknownLocation.setValue("equipment_room");
    await wrapper.findAll("button").find((item) => item.text().includes("保存结构"))!.trigger("click");
    await flushPromises();

    const sent = save.mock.calls[0][0];
    expect(sent.devices.find((device) => device.id === "pump")).toMatchObject({ floor_id: "RF", location_type: "roof" });
    expect(sent.devices.find((device) => device.id === "fcu")).toMatchObject({ location_type: "equipment_room" });
  });
});
