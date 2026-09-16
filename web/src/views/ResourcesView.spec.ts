// @vitest-environment jsdom
import { flushPromises, mount } from "@vue/test-utils";
import { beforeEach, describe, expect, it, vi } from "vitest";
import ResourcesView from "./ResourcesView.vue";
import { api } from "@/api/client";

vi.mock("@/api/client", () => ({ api: vi.fn(), errorMessage: (error: unknown) => String(error) }));
const apiMock = vi.mocked(api);
const projectState = vi.hoisted(() => ({ projectId: "p-1", selectionKey: "p-1", usingSample: false, saving: false, saveSample: vi.fn() }));
vi.mock("@/stores/project", async () => { const { reactive } = await import("vue"); return { useProjectStore: () => reactive(projectState) }; });
vi.mock("./CatalogView.vue", () => ({ default: { template: "<div data-test='catalog'>catalog</div>" } }));

const knowledge = [{ knowledge_id: "fcu", device_type: "terminal", name: "风机盘管", overview: "末端空气处理设备", catalog_linked: true, catalog_device_type: "风机盘管", applicable_systems: ["chilled_water", "air"] }];
const detail = { ...knowledge[0], principle: "水盘管与风机换热", components: [{ name: "盘管", function: "换热" }], key_parameters: [{ key: "capacity", name: "额定容量", unit: "kW", description: "额定工况下的制冷量", catalog_field: "额定容量" }] };
const scenarios = [{ id: "fan", name: "提高末端风量", description: "观察送风变化", controls: [], effects: [{ metric: "room_temperature_c", direction: "decrease" }] }];

describe("resources learning views", () => {
  beforeEach(() => {
    apiMock.mockReset();
    apiMock.mockImplementation((path: string) => path.includes("knowledge/equipment/") ? Promise.resolve(detail) : path.includes("knowledge/equipment") ? Promise.resolve(knowledge) : path.includes("teaching/scenarios") ? Promise.resolve(scenarios) : Promise.resolve({}));
  });
  it("loads knowledge cards and opens a real detail response", async () => {
    const wrapper = mount(ResourcesView);
    await flushPromises();
    await wrapper.findAll("button[role='tab']")[1].trigger("click");
    await flushPromises();
    expect(wrapper.text()).toContain("风机盘管");
    await wrapper.find(".resource-card").trigger("click");
    await flushPromises();
    expect(wrapper.text()).toContain("水盘管与风机换热");
    expect(wrapper.text()).toContain("额定容量");
  });
  it("sends the backend selection role and renders candidate eligibility", async () => {
    apiMock.mockImplementation((path: string, init?: RequestInit) => {
      if (path.includes("knowledge/equipment")) return Promise.resolve(knowledge);
      if (path.includes("teaching/scenarios")) return Promise.resolve(scenarios);
      if (path.includes("selection/recommend")) return Promise.resolve({ project_id: "p-1", target_role: "pump", status: "recommended", requirements: { cooling_load_kw: 10, required_pump_head_m: 18 }, applied_rules: { capacity_margin: { min: 1, max: 1.2 }, pump_flow_margin: { min: 1, max: 1.2 }, pump_head_margin: { min: 1, max: 1.2 } }, candidates: [{ rank: 1, device_type: "水泵", model_name: "P-1", role: "pump", quantity: 1, rated: {}, provided: {}, margins: { flow: 2, head: 3 }, eligible: true, reasons: [] }] });
      return Promise.resolve({});
    });
    const wrapper = mount(ResourcesView);
    await flushPromises();
    await wrapper.findAll("button[role='tab']")[2].trigger("click");
    await wrapper.find("select").setValue("pump");
    await wrapper.find("button.button.primary").trigger("click");
    await flushPromises();
    expect(apiMock).toHaveBeenCalledWith("/api/v1/selection/recommend/p-1", expect.objectContaining({ body: JSON.stringify({ target_role: "pump" }) }));
    expect(wrapper.text()).toContain("P-1");
    expect(wrapper.text()).toContain("可选");
    expect(wrapper.text()).toContain("扬程");
  });
  it("posts a scenario id and renders comparison metrics with convergence", async () => {
    apiMock.mockImplementation((path: string) => {
      if (path.includes("knowledge/equipment")) return Promise.resolve(knowledge);
      if (path.includes("teaching/scenarios")) return Promise.resolve(scenarios);
      if (path.includes("teaching/compare")) return Promise.resolve({ project_id: "p-1", scenario: scenarios[0], baseline: { metrics: {}, alarms: [] }, scenario_result: { metrics: {}, alarms: [] }, metrics: { average_room_temp_c: { before: 24, after: 25, change: 1, change_pct: 4.2, direction: "increase", unit: "C", applicable: true } }, explanations: [{ code: "ROOM", message: "室温升高", related_devices: [], related_metrics: [], expected_direction: "increase", observed_direction: "increase", expectation_met: true }], convergence: { baseline: { converged: true, ticks: 5, final_room_drift_c: 0.01 }, scenario: { converged: true, ticks: 6, final_room_drift_c: 0.01 } } });
      return Promise.resolve({});
    });
    const wrapper = mount(ResourcesView);
    await flushPromises();
    await wrapper.findAll("button[role='tab']")[3].trigger("click");
    await wrapper.find("button.button.primary").trigger("click");
    await flushPromises();
    expect(apiMock).toHaveBeenCalledWith("/api/v1/teaching/compare/p-1", expect.objectContaining({ body: JSON.stringify({ scenario_id: "fan" }) }));
    expect(wrapper.text()).toContain("平均室温");
    expect(wrapper.text()).toContain("已收敛");
    expect(wrapper.text()).toContain("室温升高");
  });
  it("does not apply a recommendation response after switching projects", async () => {
    let resolveRecommendation!: (value: unknown) => void;
    apiMock.mockImplementation((path: string) => {
      if (path.includes("knowledge/equipment")) return Promise.resolve(knowledge);
      if (path.includes("teaching/scenarios")) return Promise.resolve(scenarios);
      if (path.includes("selection/recommend")) return new Promise((resolve) => { resolveRecommendation = resolve; });
      return Promise.resolve({});
    });
    const wrapper = mount(ResourcesView);
    await flushPromises();
    await wrapper.findAll("button[role='tab']")[2].trigger("click");
    await wrapper.find("button.button.primary").trigger("click");
    projectState.projectId = "p-2";
    projectState.selectionKey = "p-2";
    await flushPromises();
    resolveRecommendation({ project_id: "p-1", target_role: "cold_source", status: "recommended", requirements: {}, applied_rules: { capacity_margin: { min: 1, max: 1.2 }, pump_flow_margin: { min: 1, max: 1.2 }, pump_head_margin: { min: 1, max: 1.2 } }, candidates: [] });
    await flushPromises();
    expect(wrapper.text()).not.toContain("recommended");
    projectState.projectId = "p-1";
    projectState.selectionKey = "p-1";
  });
});
