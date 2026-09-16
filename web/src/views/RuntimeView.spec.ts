// @vitest-environment jsdom

import { createPinia, setActivePinia } from "pinia";
import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { beforeEach, describe, expect, it, vi } from "vitest";
import RuntimeView from "./RuntimeView.vue";
import { useProjectStore } from "@/stores/project";
import { useRealtimeStore } from "@/stores/realtime";
import { useUiStore } from "@/stores/ui";
import type { RealtimeControlsResult, RealtimePipeStatesResult, RealtimeState } from "@/types";

const pipes: RealtimePipeStatesResult = {
  project_id: "project-a",
  running: true,
  simcore_tick: 4,
  pipes: [
    {
      pipe_id: "chw_main_supply",
      system: "chilled_water",
      pipe_type: "main_supply",
      floor_id: "B1F",
      diameter_mm: 100,
      flow_m3_h: 20.51,
      velocity_m_s: 0.725,
      pressure_drop_pa: 2104.8,
      head_m: 0.215,
      served_terminal_ids: ["fcu_f1", "fcu_f2", "fcu_f3"],
    },
    {
      pipe_id: "chw_branch_supply_f1",
      system: "chilled_water",
      pipe_type: "branch_supply",
      floor_id: "F1",
      diameter_mm: 40,
      flow_m3_h: 6.91,
      velocity_m_s: 3.2,
      pressure_drop_pa: 8210.5,
      head_m: 0.837,
      served_terminal_ids: ["fcu_f1"],
    },
    {
      pipe_id: "hw_main_supply",
      system: "hot_water",
      pipe_type: "main_supply",
      floor_id: "B1F",
      diameter_mm: 80,
      flow_m3_h: 0,
      velocity_m_s: 0,
      pressure_drop_pa: 0,
      head_m: 0,
      served_terminal_ids: [],
    },
  ],
  total_flow_by_system_m3_h: { chilled_water: 20.51, hot_water: 0 },
  most_loaded_pipe_id: "chw_main_supply",
};

const controls: RealtimeControlsResult = {
  project_id: "project-a",
  devices: {
    pump_1: {
      model_name: "EC100",
      control_source: "auto",
      controls: {
        enabled: { label: "启停", kind: "switch", unit: "", min: 0, max: 1, step: 1, current: 1 },
        speed_pct: { label: "转速", kind: "number", unit: "%", min: 0, max: 100, step: 5, current: 80 },
      },
    },
    fcu_1: { model_name: "MCW1000V", control_source: "auto", controls: {} },
  },
};

const state: RealtimeState = {
  project_id: "project-a",
  updated_at: new Date().toISOString(),
  devices: {
    pump_1: {
      project_id: "project-a",
      device_id: "pump_1",
      timestamp: "2026-08-13T03:00:00Z",
      status: "running",
      values: { role: "pump", speed_pct: 100, power_kw: Number.NaN },
    },
    fcu_1: {
      project_id: "project-a",
      device_id: "fcu_1",
      timestamp: "2026-08-13T03:00:00Z",
      status: "stopped",
      values: {},
    },
  },
  alarms: [],
  summary: {
    average_room_temp_c: 26.4,
    average_room_humidity_pct: 58.73,
    target_room_humidity_pct: 55,
  },
  history_count: 0,
};

async function mountView() {
  const pinia = createPinia();
  setActivePinia(pinia);
  const project = useProjectStore();
  const realtime = useRealtimeStore();
  project.$patch({
    active: {
      project_id: "project-a",
      name: "测试项目",
      tags: [],
      created_at: "",
      updated_at: "",
      history_count: 0,
      project: {
        floors: [{ id: "F1", name: "一层", level: 1, elevation_m: 0, height_m: 3.5 }],
        rooms: [{ id: "room-1", floor_id: "F1", name: "办公室", area_m2: 50, people: 5 }],
        devices: [
          { id: "pump_1", type: "循环泵", name: "EC100", floor_id: "F1", capacity_kw: null, flow_m3_h: null, head_m: 18, power_kw: null, quantity: 1 },
          { id: "fcu_1", type: "风机盘管", name: "MCW1000V", floor_id: "F1", quantity: 1 },
        ],
        pipes: [],
        connections: [],
        simultaneity_factor: 1,
        safety_factor: 1,
        design_options: {},
      },
      history: [],
    },
    usingSample: false,
  });
  realtime.projectId = "project-a";
  realtime.running = false;
  realtime.current = structuredClone(state);
  realtime.controls = structuredClone(controls);
  realtime.rated = {
    project_id: "project-a",
    devices: [{
      device_instance_id: "pump_1",
      device_type: "循环泵",
      model_name: "EC100",
      role: "pump",
      quantity: 1,
      source: "catalog",
      degraded_fields: [],
      values: {
        额定容量: 12,
        额定流量: 100,
        额定扬程: 20,
        输入功率: 5.5,
      },
    }],
  };
  realtime.rooms = {
    project_id: "project-a",
    running: false,
    simcore_tick: 0,
    rooms: [{
      room_id: "room-1",
      name: "办公室",
      floor_id: "F1",
      floor_name: "一层",
      temperature_c: 26,
      target_temp_c: 26,
      relative_humidity_pct: 62.5,
      target_humidity_pct: 55,
      humidity_shortfall_pct: 7.5,
      airflow_m3_h: 0,
      cooling_kw: 0,
      heating_kw: 0,
    }],
  };
  const wrapper = mount(RuntimeView, { global: { plugins: [pinia] } });
  await flushPromises();
  return { wrapper, project, realtime };
}

async function selectDevice(wrapper: VueWrapper, deviceId: string) {
  await wrapper.get("#runtime-tab-devices").trigger("click");
  const row = wrapper.findAll(".device-table tbody tr")
    .find((item) => item.text().includes(deviceId));
  expect(row, `device row ${deviceId}`).toBeTruthy();
  await row!.trigger("click");
  await flushPromises();
}

describe("runtime device controls", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("renders the equipment-card prototype from existing rated and runtime data", async () => {
    const { wrapper } = await mountView();
    const cards = wrapper.findAll(".device-card");
    expect(cards).toHaveLength(2);

    const pumpCard = cards.find((card) => card.text().includes("pump_1") || card.text().includes("EC100"));
    expect(pumpCard).toBeTruthy();
    expect(pumpCard!.text()).toContain("循环泵");
    expect(pumpCard!.text()).toContain("额定流量");
    expect(pumpCard!.text()).toContain("100.000");
    expect(pumpCard!.text()).toContain("转速");
    expect(pumpCard!.text()).toContain("100.0");
    expect(pumpCard!.text()).toContain("--");

    await pumpCard!.trigger("click");
    expect(wrapper.find(".device-identity").text()).toContain("EC100");
    wrapper.unmount();
  });

  it("switches the heat-pump card fields with the running mode", async () => {
    // 热泵冷热两用：制冷工况卡显示制冷字段（cooling_kw/reject），制热
    // 工况回到制热字段（heating_kw）。卡字段集随 summary.mode 切换。
    const pinia = createPinia();
    setActivePinia(pinia);
    const project = useProjectStore();
    const realtime = useRealtimeStore();
    const stateWithHeat = structuredClone(state);
    stateWithHeat.devices.ashp_1 = {
      project_id: "project-a",
      device_id: "ashp_1",
      timestamp: "2026-09-07T03:00:00Z",
      status: "running",
      values: {
        role: "heat_source",
        cooling_kw: 82.5,
        heating_kw: 0,
        heat_reject_kw: 108.8,
        cop: 3.14,
        load_ratio: 0.087,
        power_kw: 26.3,
        supply_temp_c: 7,
        return_temp_c: 7.9,
        flow_m3_h: 45.4,
      },
    };
    stateWithHeat.summary.mode = "cooling";
    project.$patch({
      active: {
        project_id: "project-a",
        name: "测试项目",
        tags: [],
        created_at: "",
        updated_at: "",
        history_count: 0,
        project: {
          floors: [{ id: "F1", name: "一层", level: 1, elevation_m: 0, height_m: 3.5 }],
          rooms: [{ id: "room-1", floor_id: "F1", name: "办公室", area_m2: 50, people: 5 }],
          devices: [
            { id: "pump_1", type: "循环泵", name: "EC100", floor_id: "F1", capacity_kw: null, flow_m3_h: null, head_m: 18, power_kw: null, quantity: 1 },
            { id: "fcu_1", type: "风机盘管", name: "MCW1000V", floor_id: "F1", quantity: 1 },
            { id: "ashp_1", type: "空气源热泵", name: "SHAF-270H", floor_id: "F1", capacity_kw: 947.6, flow_m3_h: 162.9, head_m: null, power_kw: 278.5, quantity: 1 },
          ],
          pipes: [],
          connections: [],
          simultaneity_factor: 1,
          safety_factor: 1,
          design_options: {},
        },
        history: [],
      },
      usingSample: false,
    });
    realtime.projectId = "project-a";
    realtime.running = false;
    realtime.current = stateWithHeat;
    realtime.controls = structuredClone(controls);
    realtime.rated = {
      project_id: "project-a",
      devices: [
        {
          device_instance_id: "pump_1",
          device_type: "循环泵",
          model_name: "EC100",
          role: "pump",
          quantity: 1,
          source: "catalog",
          degraded_fields: [],
          values: { 额定容量: 12, 额定流量: 100, 额定扬程: 20, 输入功率: 5.5 },
        },
        {
          device_instance_id: "ashp_1",
          device_type: "空气源热泵",
          model_name: "SHAF-270H",
          role: "heat_source",
          quantity: 1,
          source: "catalog",
          degraded_fields: [],
          values: { 额定容量: 947.6, 额定流量: 162.9, 性能系数: 3.4 },
        },
      ],
    };
    const wrapper = mount(RuntimeView, { global: { plugins: [pinia] } });
    await flushPromises();

    const heatCard = () => wrapper.findAll(".device-card")
      .find((card) => card.text().includes("SHAF-270H"));

    // 制冷工况：换向供冷，展示制冷字段。
    expect(heatCard()!.text()).toContain("热泵机组（制冷）");
    expect(heatCard()!.text()).toContain("当前制冷量");
    expect(heatCard()!.text()).toContain("冷凝排热");
    expect(heatCard()!.text()).not.toContain("当前制热量");

    // 制热工况：回到制热字段集。
    realtime.current = { ...stateWithHeat, summary: { ...stateWithHeat.summary, mode: "heating" } };
    await flushPromises();
    expect(heatCard()!.text()).toContain("热源机组");
    expect(heatCard()!.text()).toContain("当前制热量");
    expect(heatCard()!.text()).not.toContain("当前制冷量");
    wrapper.unmount();
  });

  it("renders switch controls as checkboxes bound to min/max values", async () => {
    const { wrapper, realtime } = await mountView();
    await selectDevice(wrapper, "pump_1");
    const spy = vi.spyOn(realtime, "setDeviceControls").mockResolvedValue(true);

    const toggle = wrapper.find("input.switch-input[type=checkbox]");
    expect(toggle.exists()).toBe(true);
    expect((toggle.element as HTMLInputElement).checked).toBe(true);

    await toggle.setValue(false);
    await wrapper.find(".device-control-actions .button.secondary").trigger("click");
    await flushPromises();

    expect(spy).toHaveBeenCalledWith("pump_1", { enabled: 0, speed_pct: 80 });
    wrapper.unmount();
  });

  it("renders numeric controls with range metadata", async () => {
    const { wrapper } = await mountView();
    await selectDevice(wrapper, "pump_1");

    const slider = wrapper.find(".numeric-control input[type=range]");
    const number = wrapper.find(".numeric-control input[type=number]");
    for (const input of [slider, number]) {
      expect(input.exists()).toBe(true);
      expect(input.attributes("min")).toBe("0");
      expect(input.attributes("max")).toBe("100");
      expect(input.attributes("step")).toBe("5");
    }
    expect((number.element as HTMLInputElement).value).toBe("80");
    wrapper.unmount();
  });

  it("shows an empty state when the selected device has no controls", async () => {
    const { wrapper } = await mountView();
    await selectDevice(wrapper, "fcu_1");

    expect(wrapper.find(".device-controls .empty-state").text()).toBe("该设备没有可用控制项。");
    expect(wrapper.find(".device-control-actions").exists()).toBe(false);
    wrapper.unmount();
  });

  it("disables saving and shows progress text while requests are in flight", async () => {
    const { wrapper, realtime } = await mountView();
    await wrapper.get("#runtime-tab-conditions").trigger("click");
    realtime.$patch({ configSaving: true });
    await flushPromises();

    const saveConfig = wrapper.find(".runtime-command-row .button.secondary");
    expect(saveConfig.text()).toContain("保存中");
    expect(saveConfig.attributes("disabled")).toBeDefined();

    await selectDevice(wrapper, "pump_1");
    realtime.$patch({ controlSavingDevice: "pump_1" });
    await flushPromises();
    const saveControls = wrapper.find(".device-control-actions .button.secondary");
    expect(saveControls.text()).toContain("保存中");
    expect(saveControls.attributes("disabled")).toBeDefined();
    wrapper.unmount();
  });

  it("surfaces store errors in the banner", async () => {
    const { wrapper, realtime } = await mountView();
    realtime.$patch({ error: "保存失败：HTTP 409" });
    await flushPromises();

    expect(wrapper.find(".error-banner").text()).toBe("保存失败：HTTP 409");
    wrapper.unmount();
  });

  it("masks non-finite runtime values in the property panel", async () => {
    const { wrapper } = await mountView();
    await selectDevice(wrapper, "pump_1");

    const runtimeGroup = wrapper.findAll(".property-group")
      .find((group) => group.find("h3").text() === "运行变量");
    expect(runtimeGroup).toBeTruthy();
    const values = runtimeGroup!.findAll("dd").map((item) => item.text());
    expect(values).toContain("100.0");
    expect(values).toContain("--");
    wrapper.unmount();
  });

  it("fills empty design attributes from rated catalog values", async () => {
    const { wrapper } = await mountView();
    await selectDevice(wrapper, "pump_1");

    const designGroup = wrapper.findAll(".property-group")
      .find((group) => group.find("h3").text() === "设计属性");
    expect(designGroup).toBeTruthy();
    const rows = designGroup!.findAll("dl > *").map((item) => item.text());
    expect(rows).toContain("12.000");
    expect(rows).toContain("100.000");
    expect(rows).toContain("18");
    expect(rows).toContain("5.500");
    wrapper.unmount();
  });

  it("renders measured humidity in the live metrics and the room table", async () => {
    const { wrapper } = await mountView();

    const humidityCell = wrapper.findAll(".live-metrics .metric-cell")
      .find((cell) => cell.text().includes("室内湿度"));
    expect(humidityCell, "室内湿度指标卡片").toBeTruthy();
    expect(humidityCell!.text()).toContain("58.7");

    await wrapper.get("#runtime-tab-conditions").trigger("click");
    const roomRow = wrapper.find(".runtime-room-table tbody tr");
    expect(roomRow.exists()).toBe(true);
    // 末列为实测湿度，输入框里的初始湿度不参与该断言
    const readouts = roomRow.findAll("td").map((cell) => cell.text());
    expect(readouts[readouts.length - 1]).toBe("62.5");
    wrapper.unmount();
  });

  it("blocks saving when target humidity is not a number", async () => {
    const { wrapper } = await mountView();
    await wrapper.get("#runtime-tab-conditions").trigger("click");
    const input = wrapper.findAll(".runtime-config-grid input")
      .find((item) => item.attributes("max") === "80");
    expect(input, "目标湿度输入框").toBeTruthy();
    expect(input!.attributes("min")).toBe("30");

    await input!.setValue("");
    await flushPromises();

    const saveButton = wrapper.findAll(".runtime-command-row button")
      .find((button) => button.text().includes("保存条件"));
    expect(saveButton!.attributes("disabled")).toBeDefined();
    wrapper.unmount();
  });
});

describe("runtime pipe readings", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("renders every pipe with system totals and marks the fastest one", async () => {
    const { wrapper, realtime } = await mountView();
    await wrapper.get("#runtime-tab-pipes").trigger("click");
    realtime.pipes = structuredClone(pipes);
    await flushPromises();

    const rows = wrapper.findAll(".pipe-content tbody tr");
    expect(rows).toHaveLength(3);
    expect(rows[0].text()).toContain("chw_main_supply");
    // 主供水管服务三台末端，服务列表用顿号连接
    expect(rows[0].text()).toContain("fcu_f1、fcu_f2、fcu_f3");
    expect(rows[0].text()).toContain("20.510");
    // 流量最高的管加高亮类，教学上先看这根
    expect(rows[0].classes()).toContain("pipe-hot");
    expect(rows[1].classes()).not.toContain("pipe-hot");

    const totals = wrapper.findAll(".pipe-total").map((item) => item.text());
    expect(totals.some((text) => text.includes("冷冻水") && text.includes("20.510"))).toBe(true);
    expect(totals.some((text) => text.includes("热水") && text.includes("0.000"))).toBe(true);
    wrapper.unmount();
  });

  it("flags over-limit velocity but leaves low-flow and zero-flow pipes unjudged", async () => {
    const { wrapper, realtime } = await mountView();
    await wrapper.get("#runtime-tab-pipes").trigger("click");
    realtime.pipes = structuredClone(pipes);
    await flushPromises();

    const rows = wrapper.findAll(".pipe-content tbody tr");
    // 3.2 m/s 高于冷冻水上限 2.5，提示管径偏小
    expect(rows[1].text()).toContain("高于 2.5");
    expect(rows[1].find(".status-badge.is-warn").exists()).toBe(true);
    // 零流量是停机的正常状态，不判超限
    expect(rows[2].text()).toContain("无流量");
    expect(rows[2].find(".status-badge.is-warn").exists()).toBe(false);
    expect(rows[2].find(".status-badge.is-muted").exists()).toBe(true);
    // 0.725 低于设计下限 0.6 之上但远未超限；部分负荷下流速偏低是常态，不能报警
    expect(rows[0].find(".status-badge.is-ok").exists()).toBe(true);
    expect(rows[0].text()).not.toContain("低于");
    wrapper.unmount();
  });

  it("keeps part-load low velocity unflagged", async () => {
    const { wrapper, realtime } = await mountView();
    await wrapper.get("#runtime-tab-pipes").trigger("click");
    const partLoad = structuredClone(pipes);
    // 水泵降频后主管流速掉到 0.195，远低于设计下限 0.6，但这是正常的部分负荷
    partLoad.pipes[0].flow_m3_h = 5.525;
    partLoad.pipes[0].velocity_m_s = 0.195;
    realtime.pipes = partLoad;
    await flushPromises();

    const row = wrapper.findAll(".pipe-content tbody tr")[0];
    expect(row.find(".status-badge.is-ok").exists()).toBe(true);
    expect(row.text()).not.toContain("低于");
    wrapper.unmount();
  });

  it("filters by system and states the derived-reading limitation", async () => {
    const { wrapper, realtime } = await mountView();
    await wrapper.get("#runtime-tab-pipes").trigger("click");
    realtime.pipes = structuredClone(pipes);
    await flushPromises();

    expect(wrapper.find(".pipe-content .table-note").text()).toContain("不求解压力平衡");

    const select = wrapper.find(".pipe-content .filter-row select");
    await select.setValue("hot_water");
    await flushPromises();

    const rows = wrapper.findAll(".pipe-content tbody tr");
    expect(rows).toHaveLength(1);
    expect(rows[0].text()).toContain("hw_main_supply");
    wrapper.unmount();
  });

  it("shows an empty state when the project has no water pipes", async () => {
    const { wrapper, realtime } = await mountView();
    await wrapper.get("#runtime-tab-pipes").trigger("click");
    realtime.pipes = {
      project_id: "project-a",
      running: false,
      simcore_tick: 0,
      pipes: [],
      total_flow_by_system_m3_h: {},
      most_loaded_pipe_id: null,
    };
    await flushPromises();

    expect(wrapper.find(".pipe-content").exists()).toBe(false);
    const emptyStates = wrapper.findAll(".empty-state").map((item) => item.text());
    expect(emptyStates.some((text) => text.includes("没有水管"))).toBe(true);
    wrapper.unmount();
  });
});


describe("humidity diagnosis", () => {
  it("shows backend humidity deviation only while the simulation is running", async () => {
    const { wrapper, realtime } = await mountView();
    realtime.running = true;
    await flushPromises();
    expect(wrapper.find(".humidity-diagnosis").exists()).toBe(true);
    expect(wrapper.find(".humidity-diagnosis").text()).toContain("后端湿度偏差");
    expect(wrapper.find(".humidity-diagnosis").text()).toContain("不能单独确定设备原因");
    wrapper.unmount();
  });

  it("stays quiet when every room is within the humidity deadband", async () => {
    const { wrapper, realtime } = await mountView();
    for (const room of realtime.rooms?.rooms ?? []) room.humidity_shortfall_pct = 1.0;
    await flushPromises();
    expect(wrapper.find(".humidity-diagnosis").exists()).toBe(false);
    wrapper.unmount();
  });
});


describe("runtime workspace navigation", () => {
  it("opens the overview with room deviations and neutral stopped connection status", async () => {
    const { wrapper } = await mountView();
    expect(wrapper.get("#runtime-tab-overview").attributes("aria-selected")).toBe("true");
    expect(wrapper.get("#runtime-panel-overview").isVisible()).toBe(true);
    expect(wrapper.get("#runtime-panel-devices").isVisible()).toBe(false);
    expect(wrapper.get(".room-comfort-card").text()).toContain("办公室");
    expect(wrapper.get(".room-comfort-card").text()).toContain("+7.5 个百分点");
    expect(wrapper.get(".runtime-status-line").find(".status-badge.is-warn").exists()).toBe(false);
    expect(wrapper.find(".humidity-diagnosis").exists()).toBe(false);
    expect(wrapper.text()).not.toContain("当前不可达");
    wrapper.unmount();
  });

  it("supports keyboard tab selection and keeps initial-condition edits across tabs", async () => {
    const { wrapper } = await mountView();
    await wrapper.get("#runtime-tab-overview").trigger("keydown", { key: "End" });
    expect(wrapper.get("#runtime-tab-conditions").attributes("aria-selected")).toBe("true");
    expect(wrapper.get("#runtime-panel-conditions").isVisible()).toBe(true);
    const targetHumidity = wrapper.findAll(".runtime-config-grid input").find((input) => input.attributes("max") === "80")!;
    await targetHumidity.setValue("62");
    await wrapper.get("#runtime-tab-overview").trigger("click");
    await wrapper.get("#runtime-tab-conditions").trigger("click");
    expect((targetHumidity.element as HTMLInputElement).value).toBe("62");
    await wrapper.get("#runtime-tab-conditions").trigger("keydown", { key: "ArrowLeft" });
    expect(wrapper.get("#runtime-tab-pipes").attributes("aria-selected")).toBe("true");
    wrapper.unmount();
  });

  it("keeps the primary simulation controls available on each workspace tab", async () => {
    const { wrapper, realtime } = await mountView();
    const start = vi.spyOn(realtime, "start").mockResolvedValue(true);
    await wrapper.get("#runtime-tab-pipes").trigger("click");
    expect(wrapper.get(".runtime-hero-actions").isVisible()).toBe(true);
    await wrapper.get(".runtime-hero-actions .button.primary").trigger("click");
    expect(start).toHaveBeenCalledWith(expect.objectContaining({ mode: "cooling", target_temp_c: 26 }));
    wrapper.unmount();
  });
});

/** 后端已经在 alarms 里给出诊断；界面必须把它翻译成"为什么全是 0"并指路。 */
function withAlarms(realtime: ReturnType<typeof useRealtimeStore>, codes: string[]) {
  realtime.running = true;
  realtime.current = {
    ...structuredClone(state),
    alarms: codes.map((code) => ({ level: "warning" as const, code, message: `${code} message`, target: "room-1" })),
  };
}

describe("runtime zero-output diagnosis", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("explains that the run cannot produce anything when no room is bound", async () => {
    const { wrapper, realtime } = await mountView();
    withAlarms(realtime, ["ROOM_BINDING_NO_DEVICE", "ROOM_BINDING_DEVICE_UNCLAIMED", "SIM_NO_CHILLED_FLOW"]);
    await flushPromises();

    const banner = wrapper.get(".runtime-diagnosis");
    expect(banner.text()).toContain("仿真跑不出任何冷热量");
    expect(banner.text()).toContain("设备读数全是 0");
    // 必须点名这是配置问题，并给出整改方向，而不是让学生自己猜
    expect(banner.text()).toContain("配置问题");
    expect(banner.text()).toContain("房间与末端");
    expect(banner.text()).toContain("去结构建模补末端");
    wrapper.unmount();
  });

  it("stays visible on every workspace tab, not only the overview", async () => {
    const { wrapper, realtime } = await mountView();
    withAlarms(realtime, ["ROOM_BINDING_NO_DEVICE", "SIM_NO_CHILLED_FLOW"]);
    await flushPromises();

    await wrapper.get("#runtime-tab-devices").trigger("click");
    expect(wrapper.find(".runtime-diagnosis").exists()).toBe(true);
    await wrapper.get("#runtime-tab-pipes").trigger("click");
    expect(wrapper.find(".runtime-diagnosis").exists()).toBe(true);
    wrapper.unmount();
  });

  it("navigates to the building editor from the banner", async () => {
    const { wrapper, realtime } = await mountView();
    withAlarms(realtime, ["ROOM_BINDING_NO_DEVICE", "SIM_NO_CHILLED_FLOW"]);
    await flushPromises();

    const ui = useUiStore();
    await wrapper.get(".runtime-diagnosis .button").trigger("click");
    expect(ui.view).toBe("building");
    wrapper.unmount();
  });

  it("downgrades to a partial warning when the run still produces cooling", async () => {
    const { wrapper, realtime } = await mountView();
    // 有房间没末端，但冷冻水有流量——仿真有产出，只是这些房间没人管
    withAlarms(realtime, ["ROOM_BINDING_NO_DEVICE"]);
    await flushPromises();

    const banner = wrapper.get(".runtime-diagnosis");
    expect(banner.classes()).toContain("partial");
    expect(banner.text()).toContain("个房间没有末端服务");
    expect(banner.text()).not.toContain("全是 0");
    wrapper.unmount();
  });

  it("shows nothing when there are no binding alarms", async () => {
    const { wrapper, realtime } = await mountView();
    withAlarms(realtime, ["SIM_DEGRADED_PARAMETERS"]);
    await flushPromises();
    expect(wrapper.find(".runtime-diagnosis").exists()).toBe(false);
    wrapper.unmount();
  });

  it("shows nothing before the simulation is started", async () => {
    const { wrapper, realtime } = await mountView();
    realtime.running = false;
    realtime.current = {
      ...structuredClone(state),
      alarms: [{ level: "warning", code: "ROOM_BINDING_NO_DEVICE", message: "x", target: "room-1" }],
    };
    await flushPromises();
    expect(wrapper.find(".runtime-diagnosis").exists()).toBe(false);
    wrapper.unmount();
  });
});
