import { createPinia, setActivePinia } from "pinia";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "@/api/client";
import { useEvaluationStore } from "./evaluation";
import type { ProjectData } from "@/types";

vi.mock("@/api/client", () => ({ api: vi.fn(), errorMessage: (value: unknown) => String(value) }));
const apiMock = vi.mocked(api);
const data: ProjectData = { floors: [], rooms: [], devices: [], pipes: [], connections: [], safety_factor: 1, simultaneity_factor: 1, design_options: {} };
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { resolve, promise };
}

describe("evaluation request isolation", () => {
  beforeEach(() => { setActivePinia(createPinia()); apiMock.mockReset(); });

  it("drops evaluation and history responses after a project reset", async () => {
    const pending = deferred<unknown>();
    const history = deferred<unknown>();
    apiMock.mockReturnValueOnce(pending.promise).mockReturnValueOnce(history.promise);
    const store = useEvaluationStore();
    const evaluating = store.evaluate("sample", data);
    const loadingHistory = store.loadHistory("old-project");
    store.reset();
    pending.resolve({ issues: [], recommendations: [] });
    history.resolve([{ history_id: "old" }]);
    await Promise.all([evaluating, loadingHistory]);
    expect(store.result).toBeNull();
    expect(store.history).toEqual([]);
    expect(store.state).toBe("idle");
  });

  it("allows energy and evaluation requests to complete independently", async () => {
    const pending = deferred<unknown>();
    const energy = deferred<unknown>();
    apiMock.mockReturnValueOnce(pending.promise).mockReturnValueOnce(energy.promise);
    const store = useEvaluationStore();
    const evaluating = store.evaluate("sample", data);
    const calculating = store.calculateEnergy(data, 24, 0.85);
    energy.resolve({ total_energy_kwh: 96 });
    pending.resolve({ issues: [], recommendations: [] });
    await Promise.all([evaluating, calculating]);
    expect(store.energy?.total_energy_kwh).toBe(96);
    expect(store.result?.issues).toEqual([]);
    expect(store.state).toBe("ready");
    expect(store.energyState).toBe("ready");
  });

  it("keeps the latest history request when an earlier one returns late", async () => {
    const first = deferred<unknown>();
    const second = deferred<unknown>();
    apiMock.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
    const store = useEvaluationStore();
    const oldRequest = store.loadHistory("a");
    const newRequest = store.loadHistory("b");
    second.resolve([{ history_id: "new" }]);
    await newRequest;
    first.resolve([{ history_id: "old" }]);
    await oldRequest;
    expect(store.history[0].history_id).toBe("new");
  });
});
