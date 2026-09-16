// @vitest-environment jsdom

import { createPinia, setActivePinia } from "pinia";
import { flushPromises, mount } from "@vue/test-utils";
import { beforeEach, describe, expect, it } from "vitest";
import HistoryView from "./HistoryView.vue";
import { useEvaluationStore } from "@/stores/evaluation";
import { useProjectStore } from "@/stores/project";
import { useRealtimeStore } from "@/stores/realtime";

function setup() {
  setActivePinia(createPinia());
  const project = useProjectStore();
  const evaluation = useEvaluationStore();
  const realtime = useRealtimeStore();
  project.$patch({
    active: {
      project_id: "sample", name: "样例", tags: [], created_at: "", updated_at: "", history_count: 0,
      project: { floors: [], rooms: [], devices: [], pipes: [], connections: [], simultaneity_factor: 1, safety_factor: 1, design_options: {} }, history: [],
    }, usingSample: true,
  });
  evaluation.history = [];
  realtime.history = [];
  return { project, evaluation, realtime };
}

function points() {
  return [
    { timestamp: "2026-09-14T08:00:00Z", summary: { mode: "cooling", average_room_temp_c: 27.2, average_room_humidity_pct: 61, total_power_kw: 12.3, device_count: 2 }, alarms: [] },
    { timestamp: "2026-09-14T08:01:00Z", summary: { mode: "cooling", average_room_temp_c: 26.4, average_room_humidity_pct: 58, total_power_kw: 10.1, device_count: 2 }, alarms: [{ code: "A1" }] },
    { timestamp: "2026-09-14T08:02:00Z", summary: { mode: "cooling", average_room_temp_c: 25.8, average_room_humidity_pct: 55, total_power_kw: 8.4, device_count: 2 }, alarms: [] },
  ];
}

describe("history and trends workspace", () => {
  beforeEach(() => setup());

  it("renders backend runtime samples as a trend and snapshots", async () => {
    const { realtime } = setup();
    realtime.history = points();
    const wrapper = mount(HistoryView);
    await flushPromises();
    expect(wrapper.find(".trend-chart").exists()).toBe(true);
    expect(wrapper.findAll(".trend-bar")).toHaveLength(3);
    expect(wrapper.text()).toContain("25.8");
    expect(wrapper.text()).toContain("运行采样");
    expect(wrapper.text()).toContain("8.40 kW");
    expect(wrapper.findAll(".table-wrap tbody tr")).toHaveLength(3);
    wrapper.unmount();
  });

  it("switches trend metrics without fabricating missing values", async () => {
    const { realtime } = setup();
    realtime.history = [
      { timestamp: "2026-09-14T08:00:00Z", summary: { average_room_temp_c: 26 }, alarms: [] },
      { timestamp: "2026-09-14T08:01:00Z", summary: { average_room_temp_c: 25.5 }, alarms: [] },
    ];
    const wrapper = mount(HistoryView);
    await flushPromises();
    await wrapper.findAll(".history-metric-tabs button")[1].trigger("click");
    expect(wrapper.find(".trend-chart").exists()).toBe(false);
    expect(wrapper.text()).toContain("没有可绘制的 平均湿度 数据");
    wrapper.unmount();
  });

  it("shows evaluation history and an explicit empty state", async () => {
    const { evaluation } = setup();
    evaluation.history = [{ history_id: "h1", project_id: "p", created_at: "2026-09-14T08:00:00Z", issue_count: 0, summary: {}, result: null }];
    const wrapper = mount(HistoryView);
    await flushPromises();
    expect(wrapper.text()).toContain("h1");
    expect(wrapper.text()).toContain("未发现问题");
    expect(wrapper.text()).toContain("尚无实时快照");
    wrapper.unmount();
  });

  it("keeps both empty states clear when there is no history", async () => {
    const wrapper = mount(HistoryView);
    await flushPromises();
    expect(wrapper.text()).toContain("没有可绘制的 平均室温 数据");
    expect(wrapper.text()).toContain("该项目还没有评估记录");
    expect(wrapper.text()).toContain("尚无实时快照");
    wrapper.unmount();
  });
});

