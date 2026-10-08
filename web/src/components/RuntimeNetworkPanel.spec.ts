// @vitest-environment jsdom

import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";
import RuntimeNetworkPanel from "./RuntimeNetworkPanel.vue";
import type { RealtimeNetworkResult, RuntimeNetworkNode } from "@/types";

function node(id: string, system: string): RuntimeNetworkNode {
  return { node_id: id, system, circuit_id: null, model_status: "modeled", flow_m3_h: 2,
    velocity_m_s: 1, pressure_drop_pa: 50, head_m: null, diameter_mm: null,
    supply_temp_c: null, return_temp_c: null, flow_blocked: false,
    served_terminal_ids: [], parameter_sources: ["rules:pressure_defaults"] };
}

describe("connected runtime network", () => {
  it("shows water and air from the same calculation and filters by medium", async () => {
    const network: RealtimeNetworkResult = { model: "bounded_teaching_network", pressure_balance_solved: false,
      circuits: [], pipes: [node("water-main", "chilled_water")],
      ducts: [node("supply-duct", "air")], devices: [], issues: [] };
    const wrapper = mount(RuntimeNetworkPanel, { props: { network, tick: 12 } });
    expect(wrapper.text()).toContain("计算步 12");
    expect(wrapper.text()).toContain("water-main");
    expect(wrapper.text()).toContain("supply-duct");
    expect(wrapper.text()).toContain("教学默认参数");
    await wrapper.get('select[aria-label="运行网络系统"]').setValue("air");
    expect(wrapper.text()).not.toContain("water-main");
    expect(wrapper.text()).toContain("supply-duct");
  });

  it("distinguishes a closed path from an unsupported calculation", () => {
    const closed = { ...node("closed", "hot_water"), flow_m3_h: 0, flow_blocked: true };
    const unsupported = { ...node("bypass", "hot_water"), model_status: "unsupported" as const, flow_m3_h: 98765 };
    const network: RealtimeNetworkResult = { model: "bounded_teaching_network", pressure_balance_solved: false,
      circuits: [], pipes: [closed, unsupported], ducts: [], devices: [],
      issues: [{ level: "warning", code: "SIM_NETWORK_UNSUPPORTED", message: "此旁通需要压力平衡求解", target: "bypass" }] };
    const wrapper = mount(RuntimeNetworkPanel, { props: { network, tick: 5 } });
    expect(wrapper.text()).toContain("已断流");
    expect(wrapper.text()).toContain("不可计算");
    expect(wrapper.text()).not.toContain("98765");
    expect(wrapper.text()).toContain("此旁通需要压力平衡求解");
  });

  it("hides numeric flow and temperatures for an unsupported circuit", () => {
    const network: RealtimeNetworkResult = { model: "bounded_teaching_network", pressure_balance_solved: false,
      circuits: [{ circuit_id: "loop", system: "chilled_water", source_ids: ["source"], drive_ids: ["pump"],
        model_status: "unsupported", flow_m3_h: 98765, supply_temp_c: 99.99, return_temp_c: 88.88 }],
      pipes: [], ducts: [], devices: [], issues: [] };
    const wrapper = mount(RuntimeNetworkPanel, { props: { network, tick: 5 } });
    expect(wrapper.text()).toContain("不可计算");
    expect(wrapper.text()).toContain("接法暂不支持");
    expect(wrapper.text()).not.toContain("98765");
    expect(wrapper.text()).not.toContain("99.99");
    expect(wrapper.text()).not.toContain("88.88");
  });
});
