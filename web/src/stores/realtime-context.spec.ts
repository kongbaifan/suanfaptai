// @vitest-environment jsdom
import { createPinia, setActivePinia } from "pinia";
import { beforeEach, expect, it, vi } from "vitest";
import { defaultSimulationConfig, useRealtimeStore } from "./realtime";

function response(body: unknown) {
  return new Response(JSON.stringify(body), { headers: { "Content-Type": "application/json" } });
}

class SocketStub {
  static instances: SocketStub[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onerror: (() => void) | null = null;
  onclose: (() => void) | null = null;
  constructor(public url: string) { SocketStub.instances.push(this); }
  close() {}
}

beforeEach(() => {
  setActivePinia(createPinia());
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  SocketStub.instances = [];
  vi.stubGlobal("WebSocket", SocketStub);
});

it("does not replace B's config with A's delayed response", async () => {
  let finish!: (value: Response) => void;
  vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>((resolve) => { finish = resolve; })));
  const store = useRealtimeStore();
  store.projectId = "project-a";
  const aConfig = { ...defaultSimulationConfig(), mode: "heating" as const, target_temp_c: 22 };
  const pending = store.saveConfig(aConfig);
  store.disconnect();
  store.projectId = "project-b";
  store.config = { ...defaultSimulationConfig(), target_temp_c: 28 };
  finish(response(aConfig));
  expect(await pending).toBeNull();
  expect({ project: store.projectId, mode: store.config.mode, target: store.config.target_temp_c })
    .toEqual({ project: "project-b", mode: "cooling", target: 28 });
});

it.each([false, true])("does not start a stale context after switching projects (back to A: %s)", async (backToA) => {
  let finishConfig!: (value: Response) => void;
  const requests: string[] = [];
  vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
    const path = String(input);
    requests.push(path);
    if (path.includes("/simulate/config/")) return new Promise<Response>((resolve) => { finishConfig = resolve; });
    return Promise.resolve(response({ running: true }));
  }));
  const store = useRealtimeStore();
  vi.spyOn(store, "syncRuntimeState").mockResolvedValue();
  store.projectId = "project-a";
  const pending = store.start(defaultSimulationConfig());
  store.disconnect();
  store.projectId = "project-b";
  if (backToA) {
    store.disconnect();
    store.projectId = "project-a";
  }
  finishConfig(response(defaultSimulationConfig()));
  expect(await pending).toBe(false);
  expect(requests.filter((path) => path.includes("/start/"))).toEqual([]);
});

it("does not change B's running state after A's stop response", async () => {
  let finish!: (value: Response) => void;
  vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>((resolve) => { finish = resolve; })));
  const store = useRealtimeStore();
  const sync = vi.spyOn(store, "syncRuntimeState").mockResolvedValue();
  store.projectId = "project-a";
  const pending = store.stop();
  store.disconnect();
  store.projectId = "project-b";
  store.running = true;
  finish(response({ running: false }));
  expect(await pending).toBe(false);
  expect(store.running).toBe(true);
  expect(sync).not.toHaveBeenCalled();
});

it("ignores old reads after returning to the same project", async () => {
  let finish!: (value: Response) => void;
  vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>((resolve) => { finish = resolve; })));
  const store = useRealtimeStore();
  store.projectId = "project-a";
  const pending = store.loadConfig();
  store.disconnect();
  store.projectId = "project-b";
  store.disconnect();
  store.projectId = "project-a";
  store.config.target_temp_c = 28;
  finish(response({ ...defaultSimulationConfig(), target_temp_c: 22 }));
  await pending;
  expect(store.config.target_temp_c).toBe(28);
});

it.each(["onopen", "onerror", "onclose"] as const)("ignores the old socket's delayed %s event", (event) => {
  const store = useRealtimeStore();
  store.projectId = "project-a";
  store.connect();
  const socketA = SocketStub.instances[0];
  store.disconnect(false);
  store.clearRuntimeData();
  store.projectId = "project-b";
  store.connect();
  const socketB = SocketStub.instances[1];
  socketB.onopen?.();
  store.reconnectAttempt = 2;
  socketA[event]?.();
  expect(store.socket).toBe(socketB);
  expect(store.socketState).toBe("connected");
  expect(store.reconnectAttempt).toBe(2);
  expect(store.error).toBe("");
  socketB.onmessage?.({ data: JSON.stringify({ type: "connected", data: { project_id: "project-b", devices: {}, simcore_tick: 2 } }) } as MessageEvent);
  expect(store.current?.simcore_tick).toBe(2);
});

it("an old activation cannot replace the socket after A to B to A", async () => {
  const store = useRealtimeStore();
  for (const method of ["loadState", "loadHistory", "loadControls", "loadRooms", "loadPipes", "loadRated"] as const) {
    vi.spyOn(store, method).mockResolvedValue();
  }
  let finishFirst!: () => void;
  vi.spyOn(store, "loadConfig")
    .mockImplementationOnce(() => new Promise<void>((resolve) => { finishFirst = resolve; }))
    .mockResolvedValue();
  const first = store.activate("project-a");
  await store.activate("project-b");
  await store.activate("project-a");
  const current = store.socket;
  finishFirst();
  await first;
  expect(store.socket).toBe(current);
  expect(SocketStub.instances).toHaveLength(2);
});
