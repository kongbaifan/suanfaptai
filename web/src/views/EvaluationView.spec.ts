// @vitest-environment jsdom

import { createPinia, setActivePinia } from "pinia";
import { flushPromises, mount } from "@vue/test-utils";
import { beforeEach, describe, expect, it, vi } from "vitest";
import EvaluationView from "./EvaluationView.vue";
import { useEvaluationStore } from "@/stores/evaluation";
import { useProjectStore } from "@/stores/project";
import { api } from "@/api/client";
import type { EvaluationResult } from "@/types";

vi.mock("@/api/client", () => ({
  api: vi.fn(),
  errorMessage: (reason: unknown) => String(reason),
}));

const apiMock = vi.mocked(api);

function evaluationWithHydraulic(overrides: Partial<EvaluationResult> = {}): EvaluationResult {
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
      total_flow_by_system_m3_h: { chilled_water: 32.0, cooling_water: 38.4, hot_water: 7.0 },
      pipes: [
        { pipe_id: "chw_main_supply", system: "chilled_water", flow_m3_h: 32.0, size: { selected_size: "DN100" }, velocity_m_s: 1.13, pressure_drop_pa: 215.4, pump_head_m: 1.2 },
        { pipe_id: "chw_branch_supply_f2", system: "chilled_water", flow_m3_h: 8.6, size: { selected_size: "DN50" }, velocity_m_s: 1.21, pressure_drop_pa: 412.7, pump_head_m: 5.7 },
        { pipe_id: "chw_branch_supply_f3", system: "chilled_water", flow_m3_h: 7.9, size: { selected_size: "DN50" }, velocity_m_s: 1.12, pressure_drop_pa: 358.2, pump_head_m: null },
        { pipe_id: "cw_main_supply", system: "cooling_water", flow_m3_h: 38.4, size: { selected_size: "DN100" }, velocity_m_s: 1.36, pressure_drop_pa: 310.9, pump_head_m: null },
        { pipe_id: "hw_main_supply", system: "hot_water", flow_m3_h: 7.0, size: { selected_size: "DN65" }, velocity_m_s: 0.62, pressure_drop_pa: 98.4, pump_head_m: null },
      ],
      most_unfavorable_pipe_id: "chw_branch_supply_f2",
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
    floors: [{ id: "F1", name: "一层", level: 1, elevation_m: 0, height_m: 3.5 }],
    rooms: [{ id: "room-1", floor_id: "F1", name: "办公室", area_m2: 50, people: 5, device_ids: [] }],
    devices: [{ id: "fcu_1", type: "风机盘管", name: "FP-34WA", floor_id: "F1", quantity: 1 }],
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

describe("evaluation hydraulic check", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("renders all pipe rows with the most-unfavorable row highlighted and pump cards", async () => {
    const { wrapper } = await mountView(evaluationWithHydraulic());
    const rows = wrapper.findAll(".band-section table tbody tr");
    expect(rows).toHaveLength(5);
    expect(wrapper.text()).toContain("chw_main_supply");
    expect(wrapper.text()).toContain("cw_main_supply");
    expect(wrapper.text()).toContain("hw_main_supply");

    // 序号列按当前视图顺序 1..n 编号
    expect(rows[0].findAll("td")[0].text()).toBe("1");
    expect(rows[1].findAll("td")[0].text()).toBe("2");
    expect(rows[4].findAll("td")[0].text()).toBe("5");

    const unfavorable = rows.find((row) => row.text().includes("chw_branch_supply_f2"));
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
    wrapper.unmount();
  });

  it("filters pipe rows by system and keeps counts in the options", async () => {
    const { wrapper } = await mountView(evaluationWithHydraulic());
    const select = wrapper.find(".hydraulic-toolbar select");
    expect(select.text()).toContain("全部（5 段）");
    expect(select.text()).toContain("冷冻水（3 段）");
    expect(select.text()).toContain("冷却水（1 段）");
    expect(select.text()).toContain("热水（1 段）");

    await select.setValue("chilled_water");
    const rows = wrapper.findAll(".band-section table tbody tr");
    expect(rows).toHaveLength(3);
    expect(rows.every((row) => row.text().includes("chw_") || row.text().includes("冷冻水"))).toBe(true);
    // 筛选后序号重新编号
    expect(rows[0].findAll("td")[0].text()).toBe("1");
    expect(rows[2].findAll("td")[0].text()).toBe("3");
    wrapper.unmount();
  });

  it("shows system total flow chips for systems with values", async () => {
    const { wrapper } = await mountView(evaluationWithHydraulic());
    const chips = wrapper.findAll(".flow-chip");
    expect(chips).toHaveLength(3);
    expect(chips[0].text()).toContain("冷冻水 32.0 m³/h");
    expect(chips[1].text()).toContain("冷却水 38.4 m³/h");
    expect(chips[2].text()).toContain("热水 7.0 m³/h");
    wrapper.unmount();
  });

  it("renders the steady-state simulation block with temperatures and energy breakdown", async () => {
    const { wrapper } = await mountView(evaluationWithHydraulic());
    const steadySection = wrapper.findAll(".band-section").find((section) => section.text().includes("稳态仿真"));
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
    expect(text).toContain("制冷量");
    expect(text).toContain("126.0 kW");
    expect(text).toContain("泵功耗");
    expect(text).toContain("4.1 kW");
    expect(text).toContain("风机功耗");
    expect(text).toContain("6.8 kW");
    expect(text).toContain("总能耗");
    expect(text).toContain("39.2 kW");
    wrapper.unmount();
  });

  it("shows empty states when hydraulic data is missing", async () => {
    const { wrapper } = await mountView(evaluationWithHydraulic({ hydraulic_network: null, simulation: null }));
    const hydraulicSection = wrapper.findAll(".band-section").find((section) => section.text().includes("水力校核"));
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
    expect(wrapper.find(".hydraulic-summary").text()).toContain("告警");
    wrapper.unmount();
  });
});
