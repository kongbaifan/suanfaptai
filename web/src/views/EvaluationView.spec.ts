// @vitest-environment jsdom

import { createPinia, setActivePinia } from "pinia";
import { flushPromises, mount } from "@vue/test-utils";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import EvaluationView from "./EvaluationView.vue";
import { useEvaluationStore } from "@/stores/evaluation";
import { useProjectStore } from "@/stores/project";
import { api } from "@/api/client";
import type { EvaluationResult, FloorLoad, RoomLoad } from "@/types";
import type { EvaluationReport } from "@/types/evaluation";

vi.mock("@/api/client", () => ({
  api: vi.fn(),
  errorMessage: (reason: unknown) => String(reason),
}));

const apiMock = vi.mocked(api);
const originalCreateUrl = Object.getOwnPropertyDescriptor(
  URL,
  "createObjectURL",
);
const originalRevokeUrl = Object.getOwnPropertyDescriptor(
  URL,
  "revokeObjectURL",
);

function mockDownload() {
  const createUrl = vi.fn((_blob: Blob) => "blob:test");
  const revokeUrl = vi.fn();
  const click = vi
    .spyOn(HTMLAnchorElement.prototype, "click")
    .mockImplementation(() => undefined);
  Object.defineProperty(URL, "createObjectURL", {
    configurable: true,
    writable: true,
    value: createUrl,
  });
  Object.defineProperty(URL, "revokeObjectURL", {
    configurable: true,
    writable: true,
    value: revokeUrl,
  });
  return { createUrl, revokeUrl, click };
}

function reportFixture(): EvaluationReport {
  return {
    summary: { room_count: 1 },
    equipment: {},
    hydraulic: null,
    air: null,
    issues: [],
    recommendations: [],
  };
}

afterEach(() => {
  if (originalCreateUrl)
    Object.defineProperty(URL, "createObjectURL", originalCreateUrl);
  else Reflect.deleteProperty(URL, "createObjectURL");
  if (originalRevokeUrl)
    Object.defineProperty(URL, "revokeObjectURL", originalRevokeUrl);
  else Reflect.deleteProperty(URL, "revokeObjectURL");
});

function evaluationWithHydraulic(
  overrides: Partial<EvaluationResult> = {},
): EvaluationResult {
  return {
    load: {
      total_cooling_kw: 120,
      total_heating_kw: 60,
      adjusted_cooling_kw: 126,
      adjusted_heating_kw: 63,
      rooms: [],
      floor_loads: [],
      issues: [],
    },
    water: { flow_m3_h: 21.7, pump_head_m: 12.6, pressure_drop_pa: 123800 },
    air: { airflow_m3_h: 24000 },
    topology: {
      node_count: 30,
      edge_count_by_system: { chilled_water: 14, hot_water: 6 },
      open_nodes_by_system: {},
      loop_count_by_system: {},
      isolated_nodes: [],
      paths: [],
      pipe_flow_m3_h: {},
      floor_summary: {},
      vertical_network: {},
      issues: [],
    },
    hydraulic_network: {
      total_flow_by_system_m3_h: {
        chilled_water: 32.0,
        cooling_water: 38.4,
        hot_water: 7.0,
      },
      pipes: [
        {
          pipe_id: "chw_main_supply",
          system: "chilled_water",
          flow_m3_h: 32.0,
          size: { selected_size: "DN100" },
          velocity_m_s: 1.13,
          pressure_drop_pa: 215.4,
          pump_head_m: 1.2,
        },
        {
          pipe_id: "chw_branch_supply_f2",
          system: "chilled_water",
          flow_m3_h: 8.6,
          size: { selected_size: "DN50" },
          velocity_m_s: 1.21,
          pressure_drop_pa: 412.7,
          pump_head_m: 5.7,
        },
        {
          pipe_id: "chw_branch_supply_f3",
          system: "chilled_water",
          flow_m3_h: 7.9,
          size: { selected_size: "DN50" },
          velocity_m_s: 1.12,
          pressure_drop_pa: 358.2,
          pump_head_m: null,
        },
        {
          pipe_id: "cw_main_supply",
          system: "cooling_water",
          flow_m3_h: 38.4,
          size: { selected_size: "DN100" },
          velocity_m_s: 1.36,
          pressure_drop_pa: 310.9,
          pump_head_m: null,
        },
        {
          pipe_id: "hw_main_supply",
          system: "hot_water",
          flow_m3_h: 7.0,
          size: { selected_size: "DN65" },
          velocity_m_s: 0.62,
          pressure_drop_pa: 98.4,
          pump_head_m: null,
        },
      ],
      paths: [
        {
          path_id: "chw_path_1",
          system: "chilled_water",
          nodes: ["chiller_1", "chw_main_supply", "fcu_1", "chw_main_return"],
          floor_ids: ["F1"],
          pipe_ids: ["chw_main_supply"],
          pressure_drop_pa: 215.4,
          head_m: 1.2,
          flow_m3_h: 32.0,
          balancing_pressure_pa: null,
          complete: true,
          closed_circuit: true,
        },
      ],
      most_unfavorable_pipe_id: "chw_branch_supply_f2",
      most_unfavorable_path_id: "chw_path_1",
      required_pump_head_m: 5.7,
      pump_check: {
        required_flow_m3_h: 32.0,
        installed_flow_m3_h: 61.6,
        required_head_m: 5.7,
        available_head_m: 15.0,
        duty_pump_count: 1,
        standby_pump_count: 1,
        flow_margin: 1.9,
        head_margin: 2.6,
        status: "ok",
      },
      balancing: {
        chw_branch_supply_f2: {
          required_pressure_pa: 412.7,
          balancing_pressure_pa: 0.0,
          status: "ok",
        },
        chw_branch_supply_f3: {
          required_pressure_pa: 358.2,
          balancing_pressure_pa: 54.5,
          status: "ok",
        },
      },
      floor_flow_summary: {
        F1: { chilled_water: 32.0, cooling_water: 38.4, hot_water: 7.0 },
      },
      issues: [],
    },
    simulation: {
      supply_water_temp_c: 7,
      return_water_temp_c: 12,
      supply_air_temp_c: 16.5,
      estimated_room_temp_c: 25.4,
      cooling_energy_kw: 126,
      pump_energy_kw: 4.1,
      fan_energy_kw: 6.8,
      total_energy_kw: 39.2,
      issues: [],
    },
    issues: [],
    recommendations: [],
    ...overrides,
  };
}

function makeProjectData() {
  return {
    floors: [
      { id: "F1", name: "一层", level: 1, elevation_m: 0, height_m: 3.5 },
    ],
    rooms: [
      {
        id: "room-1",
        floor_id: "F1",
        name: "办公室",
        area_m2: 50,
        people: 5,
        device_ids: [],
      },
    ],
    devices: [
      {
        id: "fcu_1",
        type: "风机盘管",
        name: "FP-34WA",
        floor_id: "F1",
        quantity: 1,
      },
    ],
    pipes: [],
    connections: [],
    simultaneity_factor: 1,
    safety_factor: 1,
    design_options: {},
  };
}

async function mountView(result: EvaluationResult | null) {
  const pinia = createPinia();
  setActivePinia(pinia);
  const project = useProjectStore();
  const evaluation = useEvaluationStore();
  project.$patch({
    active: {
      project_id: "project-a",
      name: "测试项目",
      tags: [],
      created_at: "",
      updated_at: "",
      history_count: 0,
      project: makeProjectData(),
      history: [],
    },
    usingSample: false,
  });
  evaluation.state = result ? "ready" : "idle";
  evaluation.result = result;
  const wrapper = mount(EvaluationView, { global: { plugins: [pinia] } });
  await flushPromises();
  return { wrapper, evaluation };
}

async function openTab(wrapper: ReturnType<typeof mount>, label: string) {
  await wrapper
    .findAll('[role="tab"]')
    .find((tab) => tab.text() === label)
    ?.trigger("click");
}

describe("evaluation hydraulic check", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("renders all pipe rows with the most-unfavorable row highlighted and pump cards", async () => {
    const { wrapper } = await mountView(evaluationWithHydraulic());
    await openTab(wrapper, "水系统");
    const rows = wrapper.findAll(".pipes-table table tbody tr");
    expect(rows).toHaveLength(5);
    expect(wrapper.text()).toContain("chw_main_supply");
    expect(wrapper.text()).toContain("cw_main_supply");
    expect(wrapper.text()).toContain("hw_main_supply");

    // 序号列按当前视图顺序 1..n 编号
    expect(rows[0].findAll("td")[0].text()).toBe("1");
    expect(rows[1].findAll("td")[0].text()).toBe("2");
    expect(rows[4].findAll("td")[0].text()).toBe("5");

    const unfavorable = rows.find((row) =>
      row.text().includes("chw_branch_supply_f2"),
    );
    expect(unfavorable).toBeTruthy();
    expect(unfavorable!.classes()).toContain("pipe-unfavorable");
    expect(unfavorable!.text()).toContain("最不利");
    expect(unfavorable!.text()).toContain("5.70 m");
    // 不在最不利路径上的管段显示占位符
    const offPath = rows.find((row) => row.text().includes("cw_main_supply"));
    expect(offPath!.text()).toContain("--");

    const summary = wrapper.find(".hydraulic-summary");
    expect(summary.text()).toContain("5.7 m");
    expect(summary.text()).toContain("chw_branch_supply_f2");
    expect(summary.text()).toContain("1.9x");
    expect(summary.text()).toContain("2.6x");
    expect(summary.text()).toContain("合格");

    // 选配对照只统计冷冻水泵：冷冻/冷却/热水各一台泵的项目在这里显示「1 用」，
    // 标题和注释必须说清，否则会被误读成台数算错了
    const pumpSection = wrapper
      .findAll(".band-section")
      .find((section) => section.text().includes("选配对照"));
    expect(pumpSection).toBeTruthy();
    expect(pumpSection!.text()).toContain("冷冻水泵选配对照");
    expect(pumpSection!.text()).toContain("只统计冷冻水泵");
    wrapper.unmount();
  });

  it("filters pipe rows by system and keeps counts in the options", async () => {
    const { wrapper } = await mountView(evaluationWithHydraulic());
    await openTab(wrapper, "水系统");
    const select = wrapper.find(".hydraulic-toolbar select");
    expect(select.text()).toContain("全部（5 段）");
    expect(select.text()).toContain("冷冻水（3 段）");
    expect(select.text()).toContain("冷却水（1 段）");
    expect(select.text()).toContain("热水（1 段）");

    await select.setValue("chilled_water");
    const rows = wrapper.findAll(".pipes-table table tbody tr");
    expect(rows).toHaveLength(3);
    expect(
      rows.every(
        (row) => row.text().includes("chw_") || row.text().includes("冷冻水"),
      ),
    ).toBe(true);
    // 筛选后序号重新编号
    expect(rows[0].findAll("td")[0].text()).toBe("1");
    expect(rows[2].findAll("td")[0].text()).toBe("3");
    wrapper.unmount();
  });

  it("renders the closed-circuit path table with the most-unfavorable path highlighted", async () => {
    const { wrapper } = await mountView(evaluationWithHydraulic());
    await openTab(wrapper, "水系统");
    const rows = wrapper.findAll(".paths-table table tbody tr");
    expect(rows).toHaveLength(1);
    expect(rows[0].text()).toContain("chw_path_1");
    expect(rows[0].text()).toContain("冷冻水");
    // 闭合环路列按 closed_circuit 渲染中文标记
    expect(rows[0].text()).toContain("是");
    expect(rows[0].text()).toContain("215 Pa");
    expect(rows[0].text()).toContain("1.20 m");
    expect(rows[0].classes()).toContain("path-unfavorable");
    expect(rows[0].text()).toContain("最不利");
    wrapper.unmount();
  });

  it("hides the path table when the network reports no paths", async () => {
    const result = evaluationWithHydraulic();
    const { wrapper } = await mountView({
      ...result,
      hydraulic_network: { ...result.hydraulic_network!, paths: [] },
    });
    await openTab(wrapper, "水系统");
    expect(wrapper.find(".paths-table").exists()).toBe(false);
    wrapper.unmount();
  });

  it("shows system total flow chips for systems with values", async () => {
    const { wrapper } = await mountView(evaluationWithHydraulic());
    await openTab(wrapper, "水系统");
    const chips = wrapper.findAll(".flow-chip");
    expect(chips).toHaveLength(3);
    expect(chips[0].text()).toContain("冷冻水 32.0 m³/h");
    expect(chips[1].text()).toContain("冷却水 38.4 m³/h");
    expect(chips[2].text()).toContain("热水 7.0 m³/h");
    wrapper.unmount();
  });

  it("renders the steady-state simulation block with temperatures and energy breakdown", async () => {
    const { wrapper } = await mountView(evaluationWithHydraulic());
    await openTab(wrapper, "稳态与能耗");
    const steadySection = wrapper
      .findAll(".band-section")
      .find((section) => section.text().includes("稳态仿真"));
    expect(steadySection).toBeTruthy();
    const text = steadySection!.text();
    expect(text).toContain("供水温度");
    expect(text).toContain("7.0 °C");
    expect(text).toContain("回水温度");
    expect(text).toContain("12.0 °C");
    expect(text).toContain("送风温度");
    expect(text).toContain("16.5 °C");
    expect(text).toContain("估算房间温度");
    expect(text).toContain("25.4 °C");
    expect(text).toContain("冷机耗功");
    expect(text).toContain("126.0 kW");
    expect(text).toContain("泵功耗");
    expect(text).toContain("4.1 kW");
    expect(text).toContain("风机功耗");
    expect(text).toContain("6.8 kW");
    expect(text).toContain("总功率");
    expect(text).toContain("39.2 kW");
    wrapper.unmount();
  });

  it("shows empty states when hydraulic data is missing", async () => {
    const { wrapper } = await mountView(
      evaluationWithHydraulic({ hydraulic_network: null, simulation: null }),
    );
    await openTab(wrapper, "水系统");
    const hydraulicSection = wrapper
      .findAll(".band-section")
      .find((section) => section.text().includes("水力校核"));
    expect(hydraulicSection).toBeTruthy();
    expect(hydraulicSection!.text()).toContain("暂无水力校核数据");
    expect(wrapper.text()).not.toContain("稳态仿真");
    wrapper.unmount();
  });

  it("shows warn badge when the pump check fails", async () => {
    const result = evaluationWithHydraulic();
    result.hydraulic_network!.pump_check = {
      required_flow_m3_h: 32,
      installed_flow_m3_h: 25,
      required_head_m: 5.7,
      available_head_m: 4.9,
      duty_pump_count: 1,
      standby_pump_count: 0,
      flow_margin: 0.8,
      head_margin: 0.9,
      status: "warning",
    };
    const { wrapper } = await mountView(result);
    await openTab(wrapper, "水系统");
    expect(wrapper.find(".hydraulic-summary").text()).toContain("告警");
    wrapper.unmount();
  });
});

describe("evaluation workspace tabs and report export", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    apiMock.mockReset();
  });

  it("exposes the five review tabs and switches to the air network panel", async () => {
    const { wrapper } = await mountView(
      evaluationWithHydraulic({
        air_network: {
          total_airflow_m3_h: 2400,
          ducts: [],
          paths: [],
          required_fan_pressure_pa: 180,
          most_unfavorable_path_id: null,
          fan_check: null,
          balancing: {
            path_count: 0,
            min_pressure_drop_pa: 0,
            max_pressure_drop_pa: 0,
            imbalance_ratio: 0,
            path_balancing_pressure_pa: {},
          },
          solver: {
            method: "directed_tree_aggregation",
            succeeded: true,
            degraded: false,
            steps: 1,
            max_flow_residual_m3_h: 0,
            flow_demand_sources: {},
            fallback_used: false,
          },
          node_balances: [],
          floor_airflow_summary: {},
          issues: [],
        } as unknown,
      }),
    );
    const tabs = wrapper.findAll('[role="tab"]');
    expect(tabs.map((tab) => tab.text())).toEqual([
      "评估结论",
      "建筑负荷",
      "水系统",
      "风系统",
      "稳态与能耗",
    ]);
    await tabs[3].trigger("click");
    expect(wrapper.find('[role="tabpanel"]').text()).toContain("风系统校核");
    wrapper.unmount();
  });

  it("posts the project to the report download endpoint", async () => {
    const { wrapper } = await mountView(evaluationWithHydraulic());
    const { createUrl, revokeUrl, click } = mockDownload();
    apiMock.mockResolvedValueOnce(reportFixture());
    await wrapper.find("button.button.secondary").trigger("click");
    await flushPromises();
    expect(apiMock).toHaveBeenCalledWith(
      "/api/v1/report/download",
      expect.objectContaining({
        method: "POST",
        body: expect.stringContaining('"floors"'),
      }),
    );
    expect(createUrl).toHaveBeenCalled();
    expect(revokeUrl).toHaveBeenCalledWith("blob:test");
    expect(click).toHaveBeenCalled();
    wrapper.unmount();
  });

  it("shows report failure without creating a download", async () => {
    const { wrapper } = await mountView(evaluationWithHydraulic());
    apiMock.mockRejectedValueOnce("服务不可用");
    await wrapper.find("button.button.secondary").trigger("click");
    await flushPromises();
    expect(wrapper.text()).toContain("报告导出失败：服务不可用");
    wrapper.unmount();
  });

  it("shows a clear empty state when no air network is returned", async () => {
    const { wrapper } = await mountView(
      evaluationWithHydraulic({ air_network: null }),
    );
    await openTab(wrapper, "风系统");
    expect(wrapper.text()).toContain("暂无风管网络结果");
    wrapper.unmount();
  });
});

/**
 * 房间负荷明细改成「楼层 → 房间」两级树：
 * 第一级是楼层（显示该层合计），第二级是该层房间；点楼层行可开合。
 */
describe("room load breakdown tree", () => {
  const COMPONENTS_F1_OFFICE = {
    people: 1,
    lighting: 1,
    equipment: 1,
    fresh_air_cooling: 1,
    latent_cooling: 2,
    solar: 1,
    envelope_cooling: 3,
    envelope_heating: 3,
    fresh_air_heating: 1,
  };
  const COMPONENTS_F1_MEETING = {
    people: 2,
    lighting: 1,
    equipment: 1,
    fresh_air_cooling: 1,
    latent_cooling: 3,
    solar: 1,
    envelope_cooling: 3,
    envelope_heating: 4,
    fresh_air_heating: 1,
  };

  function roomLoad(
    overrides: Partial<RoomLoad> & Pick<RoomLoad, "name" | "floor_id">,
  ): RoomLoad {
    return {
      id: overrides.name,
      cooling_kw: 0,
      heating_kw: 0,
      fresh_air_m3_h: 0,
      index_estimate_kw: 0,
      index_deviation_pct: 0,
      heating_index_estimate_kw: 0,
      heating_index_deviation_pct: 0,
      components_kw: {},
      ...overrides,
    };
  }

  function floorLoad(
    floor_id: string,
    floor_name: string,
    level: number,
  ): FloorLoad {
    return {
      floor_id,
      floor_name,
      level,
      area_m2: 0,
      people: 0,
      room_count: 0,
      cooling_kw: 0,
      heating_kw: 0,
      adjusted_cooling_kw: 0,
      adjusted_heating_kw: 0,
    };
  }

  function evaluationWithRooms(includeFloorLoads = true): EvaluationResult {
    return evaluationWithHydraulic({
      load: {
        total_cooling_kw: 30,
        total_heating_kw: 12,
        adjusted_cooling_kw: 29.7,
        adjusted_heating_kw: 11.88,
        rooms: [
          roomLoad({
            name: "一层办公",
            floor_id: "F1",
            cooling_kw: 10,
            heating_kw: 4,
            components_kw: COMPONENTS_F1_OFFICE,
          }),
          roomLoad({
            name: "一层会议",
            floor_id: "F1",
            cooling_kw: 12,
            heating_kw: 5,
            components_kw: COMPONENTS_F1_MEETING,
          }),
          roomLoad({
            name: "二层办公",
            floor_id: "F2",
            cooling_kw: 8,
            heating_kw: 3,
            components_kw: {
              people: 1,
              lighting: 1,
              equipment: 1,
              fresh_air_cooling: 1,
              latent_cooling: 1,
              solar: 1,
              envelope_cooling: 2,
              envelope_heating: 2,
              fresh_air_heating: 1,
            },
          }),
        ],
        floor_loads: includeFloorLoads
          ? [
              {
                ...floorLoad("F1", "一层", 1),
                area_m2: 220,
                people: 42,
                room_count: 2,
              },
              {
                ...floorLoad("F2", "二层", 2),
                area_m2: 120,
                people: 18,
                room_count: 1,
              },
            ]
          : [],
        issues: [],
      },
    });
  }

  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("groups rooms under their floor and shows floor totals", async () => {
    const { wrapper } = await mountView(evaluationWithRooms());
    await openTab(wrapper, "建筑负荷");

    const floorRows = wrapper.findAll(".load-floor-row");
    expect(floorRows).toHaveLength(2);
    expect(wrapper.findAll(".load-room-row")).toHaveLength(3);

    // 楼层已经是第一级，表里不该再有「楼层」列（否则同一信息出现两次）
    const headers = wrapper
      .findAll(".room-load-table thead th")
      .map((cell) => cell.text());
    expect(headers).not.toContain("楼层");
    expect(headers[0]).toBe("房间");

    // 第一级：楼层名 + 该层房间数/人数/面积
    expect(floorRows[0].text()).toContain("一层");
    expect(floorRows[0].text()).toContain("2 间 · 42 人 · 220 m²");
    expect(floorRows[1].text()).toContain("二层");

    // 楼层行的分项与冷/热负荷都是该层房间之和（未乘系数，与房间行同口径）
    const cells = floorRows[0].findAll("td");
    expect(cells[1].text()).toBe("3.00"); // people = 1 + 2
    expect(cells[10].text()).toBe("22.00 kW"); // 冷 = 10 + 12
    expect(cells[11].text()).toBe("9.00 kW"); // 热 = 4 + 5

    // 第二级：房间名带缩进标记，从属于所在楼层
    const roomRows = wrapper.findAll(".load-room-row");
    expect(roomRows[0].text()).toContain("一层办公");
    expect(roomRows[2].text()).toContain("二层办公");
    expect(roomRows[0].findAll("td")[10].text()).toBe("10.00 kW");

    wrapper.unmount();
  });

  it("collapses and re-expands one floor without touching the others", async () => {
    const { wrapper } = await mountView(evaluationWithRooms());
    await openTab(wrapper, "建筑负荷");

    const firstFloorToggle = wrapper.findAll(".load-tree-expand")[0];
    expect(firstFloorToggle.attributes("aria-expanded")).toBe("true");

    await firstFloorToggle.trigger("click");
    // 只收起 F1 的两间房，F2 那间仍在
    expect(wrapper.findAll(".load-room-row")).toHaveLength(1);
    expect(wrapper.findAll(".load-room-row")[0].text()).toContain("二层办公");
    expect(wrapper.findAll(".load-floor-row")).toHaveLength(2);
    expect(
      wrapper.findAll(".load-tree-expand")[0].attributes("aria-expanded"),
    ).toBe("false");

    await wrapper.findAll(".load-tree-expand")[0].trigger("click");
    expect(wrapper.findAll(".load-room-row")).toHaveLength(3);

    wrapper.unmount();
  });

  it("still groups by floor when the floor summary is missing", async () => {
    const { wrapper } = await mountView(evaluationWithRooms(false));
    await openTab(wrapper, "建筑负荷");

    // 楼层行由房间自身推断，楼层名回落到 floor_id，房间一间都不能丢
    const floorRows = wrapper.findAll(".load-floor-row");
    expect(floorRows).toHaveLength(2);
    expect(floorRows[0].text()).toContain("F1");
    expect(wrapper.findAll(".load-room-row")).toHaveLength(3);

    wrapper.unmount();
  });
});
