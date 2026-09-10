// @vitest-environment jsdom

import { createPinia, setActivePinia } from "pinia";
import { flushPromises, shallowMount } from "@vue/test-utils";
import { nextTick } from "vue";
import { describe, expect, it, vi } from "vitest";
import App from "./App.vue";
import { useDiagnosticsStore } from "@/stores/diagnostics";
import { useEvaluationStore } from "@/stores/evaluation";
import { useProjectStore } from "@/stores/project";
import { useRealtimeStore } from "@/stores/realtime";


describe("project selection", () => {
  it("resets sample-scoped state when the built-in sample changes", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const project = useProjectStore();
    const evaluation = useEvaluationStore();
    const realtime = useRealtimeStore();
    const diagnostics = useDiagnosticsStore();

    project.$patch({
      active: {
        project_id: "sample",
        name: "完整样例（未保存）",
        tags: ["sample"],
        created_at: "",
        updated_at: "",
        history_count: 0,
        project: {
          floors: [],
          rooms: [],
          devices: [],
          pipes: [],
          connections: [],
          simultaneity_factor: 1,
          safety_factor: 1,
          design_options: {},
        },
        history: [],
      },
      usingSample: true,
      activeSampleName: "complete",
    });

    vi.spyOn(project, "initialize").mockResolvedValue(undefined);
    vi.spyOn(diagnostics, "refresh").mockResolvedValue(undefined);
    const reset = vi.spyOn(evaluation, "reset");
    const disconnect = vi.spyOn(realtime, "disconnect");
    const activate = vi.spyOn(realtime, "activate");
    const wrapper = shallowMount(App, { global: { plugins: [pinia] } });
    await flushPromises();
    reset.mockClear();
    disconnect.mockClear();

    project.activeSampleName = "four_floor_fcu_fresh_air";
    await nextTick();
    await flushPromises();

    expect(project.projectId).toBe("sample");
    expect(reset).toHaveBeenCalledOnce();
    expect(disconnect).toHaveBeenCalledOnce();
    expect(activate).not.toHaveBeenCalled();

    wrapper.unmount();
  });
});
