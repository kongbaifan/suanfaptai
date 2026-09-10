// @vitest-environment jsdom

import { createPinia, setActivePinia } from "pinia";
import { flushPromises, mount } from "@vue/test-utils";
import { beforeEach, describe, expect, it, vi } from "vitest";
import BuildingView from "./BuildingView.vue";
import { useProjectStore } from "@/stores/project";
import type { CatalogItem, ProjectData, ProjectRecord } from "@/types";

vi.mock("@/api/client", () => ({
  api: vi.fn(),
  errorMessage: (reason: unknown) => String(reason),
}));

import { api } from "@/api/client";

const apiMock = vi.mocked(api);

const catalog: CatalogItem[] = [
  { 设备类型: "风机盘管", 型号: "FP-34WA", 额定容量: 3.4, 额定流量: 0.61, 输入功率: 0.086 },
  { 设备类型: "循环泵", 型号: "EC100", 额定流量: 5.2, 额定扬程: 20, 输入功率: 1.1 },
];

function projectData(overrides: Partial<ProjectData> = {}): ProjectData {
  return {
    floors: [{ id: "F1", name: "一层", level: 1, elevation_m: 0, height_m: 3.6 }],
    rooms: [{ id: "room1", floor_id: "F1", name: "教室", device_ids: [], area_m2: 60, people: 12 }],
    devices: [],
    pipes: [],
    connections: [],
    simultaneity_factor: 0.9,
    safety_factor: 1.1,
    design_options: {},
    ...overrides,
  };
}

function record(id: string, data = projectData()): ProjectRecord {
  return {
    project_id: id,
    name: `项目 ${id}`,
    description: null,
    tags: [],
    created_at: "",
    updated_at: "",
    history_count: 0,
    project: data,
    history: [],
  };
}

async function mountEditor(data = projectData()) {
  setActivePinia(createPinia());
  const store = useProjectStore();
  store.active = record("p1", data);
  store.usingSample = false;
  const wrapper = mount(BuildingView);
  await flushPromises();
  return { wrapper, store };
}

const button = (wrapper: ReturnType<typeof mount>, label: string) =>
  wrapper.findAll("button").find((item) => item.text().includes(label))!;

describe("BuildingView structural editor", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    apiMock.mockResolvedValue(catalog);
  });

  it("shows device, pipe, and connection sections scoped to the selected floor", async () => {
    const data = projectData({
      devices: [
        { id: "fcu1", type: "风机盘管", quantity: 1, floor_id: "F1" },
        { id: "pump_b1", type: "循环泵", quantity: 1, floor_id: "B1" },
      ],
      pipes: [{ id: "pipe1", system: "chilled_water", length_m: 10, diameter_mm: 40, floor_id: "F1" }],
      connections: [{ from_id: "fcu1", to_id: "pipe1", system: "chilled_water" }],
    });
    const { wrapper } = await mountEditor(data);

    expect(wrapper.text()).toContain("设备（1）");
    expect(wrapper.text()).toContain("管段（1）");
    expect(wrapper.text()).toContain("连接（1 / 1）");
  });

  it("adds a device via the modal and pre-fills rated values from the catalog", async () => {
    const { wrapper, store } = await mountEditor();

    await button(wrapper, "新增设备").trigger("click");
    expect(wrapper.find(".modal-card").exists()).toBe(true);

    await wrapper.find("select[aria-label='新增设备类型']").setValue("循环泵");
    await wrapper.find("select[aria-label='新增设备型号']").setValue("EC100");
    await button(wrapper, "添加").trigger("click");

    // 弹层关闭，设备行出现在当前层，草稿不落 store
    expect(wrapper.find(".modal-card").exists()).toBe(false);
    expect(wrapper.text()).toContain("device_1");
    expect(wrapper.text()).toContain("设备（1）");
    expect(store.active?.project.devices).toHaveLength(0);

    // 展开详情验证参数库带出的额定值
    await wrapper.find("button[aria-label='device_1 详情']").trigger("click");
    const headInput = wrapper.find('input[aria-label="device_1 扬程"]');
    expect((headInput.element as HTMLInputElement).value).toBe("20");
  });

  it("adds a pipe that lands on the selected floor with a compact row and detail", async () => {
    const { wrapper } = await mountEditor();

    await button(wrapper, "新增管路").trigger("click");

    expect(wrapper.text()).toContain("管段（1）");
    const sizeCell = wrapper.find(".pipe-size-cell");
    expect(sizeCell.text()).toBe("DN50");

    // 展开详情有局部阻力与流量
    await wrapper.find("button[aria-label='pipe_1 详情']").trigger("click");
    expect(wrapper.find('input[aria-label="pipe_1 局部阻力系数"]').exists()).toBe(true);
    expect(wrapper.find('input[aria-label="pipe_1 流量"]').exists()).toBe(true);
  });

  it("shows air pipes with width×height sizing and duct fields in detail", async () => {
    const { wrapper } = await mountEditor(projectData({
      pipes: [{ id: "duct1", system: "air", length_m: 8, width_mm: 320, height_mm: 250, floor_id: "F1" }],
    }));

    expect(wrapper.find(".pipe-size-cell").text()).toBe("320×250");
    await wrapper.find("button[aria-label='duct1 详情']").trigger("click");
    expect(wrapper.find('input[aria-label="duct1 宽度"]').exists()).toBe(true);
    expect(wrapper.find('input[aria-label="duct1 管径"]').exists()).toBe(false);
  });

  it("offers grouped select options for connection endpoints", async () => {
    const { wrapper } = await mountEditor(projectData({
      devices: [{ id: "fcu1", type: "风机盘管", quantity: 1, floor_id: "F1" }],
      pipes: [{ id: "pipe1", system: "chilled_water", length_m: 10, diameter_mm: 40, floor_id: "F1" }],
      connections: [{ from_id: "fcu1", to_id: "pipe1", system: "chilled_water" }],
    }));

    const startSelect = wrapper.find("select[aria-label='连接 1 起点']");
    const optionValues = startSelect.findAll("option").map((option) => option.element as HTMLOptionElement).map((o) => o.value);
    expect(optionValues).toContain("fcu1");
    expect(optionValues).toContain("pipe1");
    expect((startSelect.element as HTMLSelectElement).value).toBe("fcu1");

    // 通过下拉切换终点到自身，触发自连校验
    const endSelect = wrapper.find("select[aria-label='连接 1 终点']");
    await endSelect.setValue("fcu1");
    expect(wrapper.text()).toContain("连接的起点和终点不能相同");
    expect(button(wrapper, "保存结构").attributes("disabled")).toBeDefined();
  });

  it("validates invalid pipe dimensions before save", async () => {
    const { wrapper } = await mountEditor(projectData({
      devices: [{ id: "fcu1", type: "风机盘管", quantity: 1 }],
      pipes: [{ id: "pipe1", system: "chilled_water", length_m: 10, diameter_mm: 40, floor_id: "F1" }],
      connections: [{ from_id: "fcu1", to_id: "pipe1", system: "chilled_water" }],
    }));

    const length = wrapper.find('input[aria-label="pipe1 长度"]');
    await length.setValue("-1");
    expect(wrapper.text()).toContain("管路长度必须是大于或等于 0");
    expect(button(wrapper, "保存结构").attributes("disabled")).toBeDefined();
  });

  it("groups risers into a dedicated view instead of every floor", async () => {
    const { wrapper } = await mountEditor(projectData({
      pipes: [
        { id: "pipe_f1", system: "chilled_water", pipe_type: "branch_supply", length_m: 6, diameter_mm: 50, floor_id: "F1" },
        { id: "chw_riser", system: "chilled_water", pipe_type: "riser_supply", length_m: 16, diameter_mm: 80 },
      ],
      connections: [
        { from_id: "chw_riser", to_id: "pipe_f1", system: "chilled_water" },
      ],
    }));

    // 楼层视图：只有本层管段，不含立管
    expect(wrapper.text()).toContain("管段（1）");
    expect(wrapper.text()).not.toContain("chw_riser");

    // 立管入口存在，点进去是立管视图
    const riserNav = wrapper.findAll(".floor-nav-item").find((item) => item.text().includes("立管"))!;
    expect(riserNav.text()).toContain("1 根");
    await riserNav.trigger("click");

    expect(wrapper.text()).toContain("立管（1）");
    expect(wrapper.text()).toContain("chw_riser");
    expect(wrapper.text()).toContain("跨层连接（1 / 1）");
    // 楼层详情头部不再显示（立管视图没有 FLOOR 头）
    expect(wrapper.text()).not.toContain("层高");
  });

  it("keeps floor connection lists free of riser-hubbed links", async () => {
    const { wrapper } = await mountEditor(projectData({
      devices: [{ id: "fcu1", type: "风机盘管", quantity: 1, floor_id: "F1" }],
      pipes: [
        { id: "pipe_f1", system: "chilled_water", pipe_type: "branch_supply", length_m: 6, diameter_mm: 50, floor_id: "F1" },
        { id: "chw_riser", system: "chilled_water", pipe_type: "riser_supply", length_m: 16, diameter_mm: 80 },
      ],
      connections: [
        { from_id: "chw_riser", to_id: "pipe_f1", system: "chilled_water" },
        { from_id: "pipe_f1", to_id: "fcu1", system: "chilled_water" },
      ],
    }));

    // 楼层视图只显示两端都在本层的连接（立管连接归立管视图）
    expect(wrapper.text()).toContain("连接（1 / 2）");
  });

  it("sends the complete canonical project on a successful save round-trip", async () => {
    const { wrapper, store } = await mountEditor();
    const update = vi.spyOn(store, "updateProject").mockResolvedValue(true);

    await button(wrapper, "新增设备").trigger("click");
    await button(wrapper, "添加").trigger("click");
    await button(wrapper, "新增管路").trigger("click");
    await button(wrapper, "新增连接").trigger("click");
    await wrapper.find('input[aria-label="device_1 数量"]').setValue("2");
    await button(wrapper, "保存结构").trigger("click");
    await flushPromises();

    expect(update).toHaveBeenCalledOnce();
    const sent = update.mock.calls[0][0];
    expect(sent).toMatchObject({
      floors: expect.any(Array),
      rooms: expect.any(Array),
      devices: [{ id: "device_1", type: "风机盘管", quantity: 2 }],
      pipes: [{ id: "pipe_1", system: "chilled_water", floor_id: "F1" }],
      connections: [expect.objectContaining({ system: "chilled_water" })],
    });
  });

  it("resets the draft when the selected project changes", async () => {
    const { wrapper, store } = await mountEditor(projectData({
      devices: [{ id: "old_device", type: "风机盘管", quantity: 1 }],
    }));
    await button(wrapper, "新增管路").trigger("click");
    expect(wrapper.text()).toContain("管段（1）");

    store.active = record("p2", projectData({
      devices: [{ id: "new_device", type: "循环泵", quantity: 1 }],
    }));
    await flushPromises();

    expect(wrapper.text()).toContain("new_device");
    expect(wrapper.text()).not.toContain("old_device");
    expect(wrapper.text()).toContain("管段（0）");
  });
});
