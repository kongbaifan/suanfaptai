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

function projectData(overrides: Partial<ProjectData> = {}): ProjectData {
  return {
    floors: [
      { id: "F1", name: "一层", level: 1, elevation_m: 0, height_m: 3.6 },
      { id: "F2", name: "二层", level: 2, elevation_m: 3.6, height_m: 3.6 },
    ],
    rooms: [
      { id: "r_office", floor_id: "F1", name: "办公区", device_ids: [], area_m2: 180, people: 30 },
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

function record(data: ProjectData): ProjectRecord {
  return {
    project_id: "p1",
    name: "测试项目",
    description: null,
    tags: [],
    created_at: "",
    updated_at: "",
    history_count: 0,
    project: data,
    history: [],
  };
}

async function mountWith(data = projectData(), usingSample = false) {
  setActivePinia(createPinia());
  const store = useProjectStore();
  store.active = record(data);
  store.usingSample = usingSample;
  const wrapper = mount(BuildingView);
  await flushPromises();
  return { wrapper, store };
}

const byText = (wrapper: Awaited<ReturnType<typeof mountWith>>["wrapper"], text: string) =>
  wrapper.findAll("button").find((item) => item.text().includes(text))!;

describe("BuildingView floor navigation", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    apiMock.mockResolvedValue([]);
  });

  it("renders one nav item per floor and defaults to the first floor", async () => {
    const { wrapper } = await mountWith();

    const navItems = wrapper.findAll(".floor-nav-item");
    expect(navItems).toHaveLength(2);
    expect(navItems[0].classes()).toContain("active");
    expect(navItems[0].text()).toContain("一层");
    expect(navItems[0].text()).toContain("F1 · 1 房间");

    // 默认选中第一层：详情头部显示一层，房间卡片是第一层的
    expect(wrapper.find(".floor-head h2").text()).toBe("一层");
    expect(wrapper.findAll(".room-card")).toHaveLength(1);
    const roomName = wrapper.find(".room-card").find('input[aria-label="r_office 名称"]');
    expect((roomName.element as HTMLInputElement).value).toBe("办公区");
    expect(wrapper.text()).toContain("2 层");
    expect(wrapper.text()).toContain("2 间");
  });

  it("switches the detail panel when another floor is selected", async () => {
    const { wrapper } = await mountWith();

    await wrapper.findAll(".floor-nav-item")[1].trigger("click");

    expect(wrapper.find(".floor-head h2").text()).toBe("二层");
    const roomName = wrapper.find(".room-card").find('input[aria-label="r_training 名称"]');
    expect((roomName.element as HTMLInputElement).value).toBe("培训室");
  });

  it("adds a floor to the draft without saving it and selects it", async () => {
    const { wrapper, store } = await mountWith();
    const update = vi.spyOn(store, "updateProject").mockResolvedValue(true);

    await byText(wrapper, "新增楼层").trigger("click");

    expect(wrapper.findAll(".floor-nav-item")).toHaveLength(3);
    // 新楼层自动成为选中层
    expect(wrapper.find(".floor-head h2").text()).toContain("3 层");
    expect(update).not.toHaveBeenCalled();
    expect(store.active!.project.floors).toHaveLength(2);
  });

  it("adds a room to the selected floor without saving", async () => {
    const { wrapper } = await mountWith();

    await wrapper.findAll(".floor-nav-item")[1].trigger("click");
    const addRoomButton = wrapper.findAll("button").find(
      (item) => item.text().trim() === "房间",
    )!;
    await addRoomButton.trigger("click");

    expect(wrapper.findAll(".room-card")).toHaveLength(2);
  });

  it("sends the edited structure on save", async () => {
    const { wrapper, store } = await mountWith();
    const update = vi.spyOn(store, "updateProject").mockResolvedValue(true);

    await byText(wrapper, "新增楼层").trigger("click");
    await byText(wrapper, "保存结构").trigger("click");
    await flushPromises();

    expect(update).toHaveBeenCalledTimes(1);
    expect(update.mock.calls[0][0].floors).toHaveLength(3);
  });

  it("restores the saved structure on 还原", async () => {
    const { wrapper } = await mountWith();

    await byText(wrapper, "新增楼层").trigger("click");
    expect(wrapper.findAll(".floor-nav-item")).toHaveLength(3);
    await byText(wrapper, "还原").trigger("click");

    expect(wrapper.findAll(".floor-nav-item")).toHaveLength(2);
  });

  it("warns that deleting a floor takes its rooms, and obeys a cancel", async () => {
    const { wrapper } = await mountWith();
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(false);

    await wrapper.find(".floor-head .icon-button.danger").trigger("click");

    expect(confirmSpy.mock.calls[0][0]).toContain("1 个房间及其末端绑定会一并删除");
    expect(wrapper.findAll(".floor-nav-item")).toHaveLength(2);
  });

  it("names the terminals bound to a room before deleting it", async () => {
    const data = projectData({
      devices: [{ id: "ahu_f1", type: "组合式空调机组", quantity: 1, floor_id: "F1" }],
    });
    data.rooms[0].device_ids = ["ahu_f1"];
    const { wrapper } = await mountWith(data);
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);

    await wrapper.find(".room-card .icon-button.danger").trigger("click");

    expect(confirmSpy.mock.calls[0][0]).toContain("ahu_f1");
    expect(confirmSpy.mock.calls[0][0]).toContain("不再服务它");
  });

  it("shows bound terminals as chips and unbinds on chip click", async () => {
    const data = projectData({
      devices: [{ id: "ahu_f1", type: "组合式空调机组", quantity: 1, floor_id: "F1" }],
    });
    data.rooms[0].device_ids = ["ahu_f1"];
    const { wrapper } = await mountWith(data);

    const chip = wrapper.find(".terminal-chip");
    expect(chip.text()).toContain("ahu_f1");
    await chip.trigger("click");

    expect(wrapper.find(".terminal-chip").exists()).toBe(false);
    expect(wrapper.text()).toContain("＋ 绑定末端");
  });

  it("binds a terminal via the add-terminal dropdown", async () => {
    const data = projectData({
      devices: [{ id: "ahu_f1", type: "组合式空调机组", quantity: 1, floor_id: "F1" }],
    });
    const { wrapper } = await mountWith(data);

    await wrapper.find("select[aria-label='办公区 添加末端']").setValue("ahu_f1");

    expect(wrapper.find(".terminal-chip").text()).toContain("ahu_f1");
  });

  it("offers mall-family room purposes and patches the draft on change", async () => {
    const { wrapper, store } = await mountWith();
    const update = vi.spyOn(store, "updateProject").mockResolvedValue(true);

    const purposeSelect = wrapper.find("select[aria-label='办公区 用途']");
    expect(purposeSelect.exists()).toBe(true);
    // 商场类细分用途都在选项里
    const options = purposeSelect.findAll("option").map((o) => (o.element as HTMLOptionElement).value);
    for (const purpose of ["office", "meeting", "classroom", "hotel", "mall", "supermarket", "restaurant", "atrium", "shop"]) {
      expect(options).toContain(purpose);
    }
    // 选项带指标摘要
    expect(purposeSelect.text()).toContain("超市");
    expect(purposeSelect.text()).toContain("冷 200 W/m²");

    await purposeSelect.setValue("supermarket");
    await byText(wrapper, "保存结构").trigger("click");
    await flushPromises();

    expect(update).toHaveBeenCalledOnce();
    expect(update.mock.calls[0][0].rooms[0]).toMatchObject({ purpose: "supermarket" });
  });

  it("blocks saving while any area is non-positive", async () => {
    const { wrapper } = await mountWith();

    const areaInput = wrapper.find('input[aria-label="办公区 面积（m²）"]');
    await areaInput.setValue("0");

    expect(wrapper.text()).toContain("面积必须为大于 0 的数值");
    expect(byText(wrapper, "保存结构").attributes("disabled")).toBeDefined();
  });

  it("blocks saving while a room name is blank", async () => {
    const { wrapper } = await mountWith();

    await wrapper.find('input[aria-label="r_office 名称"]').setValue("   ");

    expect(wrapper.text()).toContain("房间名称不能为空");
    expect(byText(wrapper, "保存结构").attributes("disabled")).toBeDefined();
  });

  it("flags rooms pointing at a missing floor", async () => {
    const { wrapper } = await mountWith(projectData({
      rooms: [{ id: "r_ghost", floor_id: "F9", name: "孤儿", device_ids: [], area_m2: 70, people: 1 }],
    }));

    expect(wrapper.find(".orphan-block").exists()).toBe(true);
    expect(wrapper.text()).toContain("UNKNOWN_ROOM_FLOOR");
  });

  it("offers no editing for an unsaved sample", async () => {
    const { wrapper } = await mountWith(projectData(), true);

    expect(wrapper.find(".builder-layout").exists()).toBe(false);
    expect(wrapper.text()).toContain("样例项目不能直接编辑");
  });

  it("surfaces the backend save error", async () => {
    const { wrapper, store } = await mountWith();
    vi.spyOn(store, "updateProject").mockImplementation(async () => {
      store.saveError = "Device ahu_f1 must serve at least one room.";
      return false;
    });

    await byText(wrapper, "新增楼层").trigger("click");
    await byText(wrapper, "保存结构").trigger("click");
    await flushPromises();

    expect(wrapper.find(".error-banner").text()).toContain("must serve at least one room");
  });
});
