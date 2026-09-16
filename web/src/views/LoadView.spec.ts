// @vitest-environment jsdom

import { createPinia, setActivePinia } from "pinia";
import { flushPromises, mount } from "@vue/test-utils";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "@/api/client";
import { useLoadStore } from "@/stores/load";
import { useProjectStore } from "@/stores/project";
import type { LoadResult, ProjectData } from "@/types";
import LoadView from "./LoadView.vue";

vi.mock("@/api/client", () => ({ api: vi.fn(), errorMessage: (error: unknown) => String(error) }));
const apiMock = vi.mocked(api);

/** 分项之和恒等于总冷负荷——与后端 `load.py` 的守恒约定一致。 */
const loadResult: LoadResult = {
  total_cooling_kw: 30.5,
  total_heating_kw: 12,
  adjusted_cooling_kw: 30.2,
  adjusted_heating_kw: 11.88,
  rooms: [
    {
      id: "r1", floor_id: "F1", name: "办公区", cooling_kw: 20.5, heating_kw: 8,
      fresh_air_m3_h: 900, index_estimate_kw: 23, index_deviation_pct: -10.9,
      heating_index_estimate_kw: 15, heating_index_deviation_pct: -46.7,
      components_kw: {
        people: 3.6, lighting: 2, equipment: 3, fresh_air_cooling: 5,
        latent_cooling: 6, solar: 0.5, envelope_cooling: 0.4,
        envelope_heating: 6, fresh_air_heating: 6,
      },
    },
    {
      id: "r2", floor_id: "F1", name: "会议室", cooling_kw: 10, heating_kw: 4,
      fresh_air_m3_h: 700, index_estimate_kw: 13.5, index_deviation_pct: -25.9,
      heating_index_estimate_kw: 7.65, heating_index_deviation_pct: -47.7,
      components_kw: {
        people: 2.6, lighting: 1.1, equipment: 0.9, fresh_air_cooling: 2,
        latent_cooling: 2.5, solar: 0.5, envelope_cooling: 0.4,
        envelope_heating: 2.65, fresh_air_heating: 4.35,
      },
    },
  ],
  floor_loads: [
    {
      floor_id: "F1", floor_name: "一层", level: 1, area_m2: 290, people: 52,
      room_count: 2, cooling_kw: 30.5, heating_kw: 12,
      adjusted_cooling_kw: 30.2, adjusted_heating_kw: 11.88,
    },
  ],
  issues: [],
};

function projectData(overrides: Partial<ProjectData> = {}): ProjectData {
  return {
    floors: [{ id: "F1", name: "一层", level: 1, elevation_m: 0, height_m: 3.9 }],
    rooms: [
      {
        id: "r1", floor_id: "F1", name: "办公区", device_ids: ["fcu_1"],
        area_m2: 200, people: 30, purpose: "office",
        outdoor_summer_c: 35, indoor_summer_c: 26,
        outdoor_winter_c: -5, indoor_winter_c: 20,
        indoor_relative_humidity: 0.55, outdoor_relative_humidity: 0.65,
      },
      {
        id: "r2", floor_id: "F1", name: "会议室", device_ids: ["fcu_1"],
        area_m2: 90, people: 22, purpose: "meeting",
        outdoor_summer_c: 35, indoor_summer_c: 26,
        outdoor_winter_c: -5, indoor_winter_c: 20,
        indoor_relative_humidity: 0.55, outdoor_relative_humidity: 0.65,
      },
    ],
    devices: [], pipes: [], connections: [],
    simultaneity_factor: 0.9, safety_factor: 1.1, design_options: {},
    ...overrides,
  };
}

function setup(project: ProjectData | null = projectData()) {
  setActivePinia(createPinia());
  const projectStore = useProjectStore();
  const load = useLoadStore();
  // 直接赋值而不是 `$patch`：`RoomInput` 带字符串索引签名，Pinia 的
  // `_DeepPartial` 与它不兼容，`$patch` 会直接编译不过。
  projectStore.active = project === null ? null : {
    project_id: "p-1", name: "教学项目", tags: [], created_at: "", updated_at: "",
    history_count: 0, project, history: [],
  };
  projectStore.usingSample = false;
  return { projectStore, load };
}

/** 取出最后一次负荷计算请求的 body。 */
function lastPayload() {
  const call = apiMock.mock.calls.filter(([path]) => path === "/api/v1/load/calculate").at(-1);
  expect(call).toBeTruthy();
  return JSON.parse(String((call![1] as RequestInit).body));
}

describe("load calculation workspace", () => {
  beforeEach(() => {
    apiMock.mockReset();
    apiMock.mockResolvedValue(loadResult);
  });

  it("seeds from the current project rooms and computes with them", async () => {
    setup();
    const wrapper = mount(LoadView);
    await flushPromises();

    expect(apiMock).toHaveBeenCalledWith("/api/v1/load/calculate", expect.objectContaining({ method: "POST" }));
    const payload = lastPayload();
    expect(payload.rooms).toHaveLength(2);
    expect(payload.rooms[0].name).toBe("办公区");
    expect(payload.rooms[0].area_m2).toBe(200);
    expect(payload.simultaneity_factor).toBe(0.9);
    expect(payload.safety_factor).toBe(1.1);

    expect(wrapper.text()).toContain("负荷计算");
    expect(wrapper.text()).toContain("教学项目");
    expect(wrapper.text()).toContain("30.50 kW");
    wrapper.unmount();
  });

  it("renders the itemised breakdown and the conservation check", async () => {
    setup();
    const wrapper = mount(LoadView);
    await flushPromises();

    // 分项按固定顺序、用中文名渲染，且不含对象键序造成的抖动。
    const names = wrapper.findAll(".component-name").map((node) => node.text());
    expect(names).toEqual([
      "人员", "照明", "设备", "新风显热", "新风潜热", "太阳辐射", "围护传热(冷)",
      "围护传热(热)", "新风加热",
    ]);
    // 冷负荷分项合计 = 人员+照明+设备+新风显热+新风潜热+太阳辐射+围护传热(冷) = 30.5
    expect(wrapper.text()).toContain("冷负荷分项合计 30.50 kW");
    expect(wrapper.text()).toContain("守恒");
    // 供暖两项单列，不计入冷负荷。
    expect(wrapper.text()).toContain("不计入冷负荷");
    wrapper.unmount();
  });

  it("recomputes with the edited room value", async () => {
    const { projectStore } = setup();
    const wrapper = mount(LoadView);
    await flushPromises();
    expect(apiMock).toHaveBeenCalledTimes(1);

    await wrapper.findAll('[data-field="area_m2"]')[0].setValue("260");
    // 输入用 @change 提交，vue-test-utils 的 setValue 会同时触发 input 与 change。
    const load = useLoadStore();
    expect(load.rooms[0].area_m2).toBe(260);
    load.cancelScheduled();
    await load.recalculate();
    await flushPromises();

    expect(lastPayload().rooms[0].area_m2).toBe(260);
    // 底稿是内存副本：改参数不能写回项目。
    expect(projectStore.project?.rooms[0].area_m2).toBe(200);
    wrapper.unmount();
  });

  it("applies a design parameter to every room at once", async () => {
    setup();
    const wrapper = mount(LoadView);
    await flushPromises();

    await wrapper.find('[data-field="outdoor_summer_c"]').setValue("38");
    const load = useLoadStore();
    expect(load.rooms.map((room) => room.outdoor_summer_c)).toEqual([38, 38]);
    load.cancelScheduled();
    wrapper.unmount();
  });

  it("flags rooms whose design parameters disagree instead of pretending they match", async () => {
    const project = projectData();
    project.rooms[1].outdoor_summer_c = 33;
    setup(project);
    const wrapper = mount(LoadView);
    await flushPromises();

    expect(wrapper.text()).toContain("各房间取值不一致");
    expect(wrapper.text()).toContain("夏季室外温度");
    wrapper.unmount();
  });

  it("restores the project baseline after edits", async () => {
    setup();
    const wrapper = mount(LoadView);
    await flushPromises();
    const load = useLoadStore();

    await wrapper.findAll('[data-field="area_m2"]')[0].setValue("500");
    load.cancelScheduled();
    expect(load.rooms[0].area_m2).toBe(500);

    await wrapper.find("button.button.secondary").trigger("click");
    await flushPromises();
    expect(load.rooms[0].area_m2).toBe(200);
    expect(lastPayload().rooms[0].area_m2).toBe(200);
    wrapper.unmount();
  });

  it("keeps the newest result when an older request resolves late", async () => {
    const { load } = setup();
    // 不挂载视图，因此要自己播种——否则草稿为空，`recalculate` 会直接返回。
    load.seedFromProject(projectData(), "p-1");
    expect(load.rooms).toHaveLength(2);
    let resolveFirst!: (value: LoadResult) => void;
    apiMock
      .mockImplementationOnce(() => new Promise((resolve) => { resolveFirst = resolve; }))
      .mockResolvedValue({ ...loadResult, total_cooling_kw: 99, adjusted_cooling_kw: 98 });

    const first = load.recalculate();
    const second = load.recalculate();
    await second;
    expect(load.result?.total_cooling_kw).toBe(99);

    // 先发的那次现在才回来，必须被丢弃。
    resolveFirst(loadResult);
    await first;
    expect(load.result?.total_cooling_kw).toBe(99);
  });

  it("shows an explicit empty state when no project is selected", async () => {
    setup(null);
    const wrapper = mount(LoadView);
    await flushPromises();

    expect(wrapper.text()).toContain("还没有可算的房间");
    expect(wrapper.text()).toContain("以当前项目的房间为底稿");
    expect(apiMock).not.toHaveBeenCalled();
    wrapper.unmount();
  });

  it("surfaces a backend rejection instead of keeping a stale result", async () => {
    setup();
    apiMock.mockRejectedValue(new Error("面积必须大于 0"));
    const wrapper = mount(LoadView);
    await flushPromises();

    expect(wrapper.find(".error-banner").text()).toContain("面积必须大于 0");
    wrapper.unmount();
  });
});
