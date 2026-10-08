// @vitest-environment jsdom

import { createPinia, setActivePinia } from "pinia";
import { reactive } from "vue";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { copySimulationConfig, useRealtimeStore } from "./realtime";
import type {
  RatedDevicesResult,
  RealtimeControlsResult,
  RealtimePipeStatesResult,
  SimulationConfig,
} from "@/types";

class WebSocketStub {
  onopen: (() => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onerror: (() => void) | null = null;
  onclose: (() => void) | null = null;
  close() {}
}

const config: SimulationConfig = {
  mode: "cooling",
  interval_seconds: 1,
  initial_room_temp_c: null,
  room_initial_conditions: [],
  target_temp_c: 26,
  target_humidity_pct: 55,
  outdoor_base_temp_c: 33,
  outdoor_relative_humidity_pct: 70,
  fault_mode: "none",
};

const controls: RealtimeControlsResult = {
  project_id: "project-a",
  devices: {
    pump: {
      model_name: "EC100",
      control_source: "auto",
      controls: {
        speed_pct: {
          label: "转速",
          kind: "number",
          unit: "%",
          min: 0,
          max: 100,
          step: 1,
          current: 100,
        },
      },
    },
  },
};

const rated: RatedDevicesResult = {
  project_id: "project-a",
  devices: [
    {
      device_instance_id: "pump",
      device_type: "循环泵",
      model_name: "EC100",
      role: "pump",
      quantity: 1,
      source: "catalog",
      degraded_fields: [],
      values: { 额定流量: 100, 变频能力: true },
    },
  ],
};

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
      served_terminal_ids: ["fcu_f1"],
    },
  ],
  total_flow_by_system_m3_h: { chilled_water: 20.51 },
  most_loaded_pipe_id: "chw_main_supply",
};

function response(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("realtime runtime inputs", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    vi.restoreAllMocks();
    vi.stubGlobal("WebSocket", WebSocketStub);
  });

  it("copies Vue reactive simulation config into a plain writable draft", () => {
    const proxied = reactive({
      ...config,
      room_initial_conditions: [
        { room_id: "room-1", temperature_c: 25, relative_humidity_pct: 55 },
      ],
    });

    const copied = copySimulationConfig(proxied);

    expect(copied).toEqual(proxied);
    expect(copied).not.toBe(proxied);
    expect(copied.room_initial_conditions[0]).not.toBe(
      proxied.room_initial_conditions[0],
    );
  });

  it("loads state, history, config, controls, rooms, pipes and rated devices on activation", async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const path = String(input);
      if (path.includes("/state/")) return Promise.resolve(response({ project_id: "project-a", devices: {}, alarms: [], summary: {}, history_count: 0 }));
      if (path.includes("/history/")) return Promise.resolve(response({ points: [] }));
      if (path.includes("/simulate/config/")) return Promise.resolve(response(config));
      if (path.includes("/controls/")) return Promise.resolve(response(controls));
      if (path.includes("/rooms/")) return Promise.resolve(response({ project_id: "project-a", running: true, simcore_tick: 4, rooms: [] }));
      if (path.includes("/pipes/")) return Promise.resolve(response(pipes));
      if (path.includes("/devices/rated")) return Promise.resolve(response(rated));
      throw new Error(`Unexpected path ${path}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const store = useRealtimeStore();

    await store.activate("project-a");

    expect(fetchMock).toHaveBeenCalledTimes(7);
    expect(store.config).toEqual(config);
    expect(store.controls).toEqual(controls);
    expect(store.rooms?.simcore_tick).toBe(4);
    expect(store.pipes).toEqual(pipes);
    expect(store.rated).toEqual(rated);
    expect(store.running).toBe(true);
  });

  it("writes back clamped controls and preserves confirmed values after failure", async () => {
    const accepted = structuredClone(controls);
    accepted.devices.pump.control_source = "manual_override";
    accepted.devices.pump.controls.speed_pct.current = 73;
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(response(accepted))
      .mockResolvedValueOnce(response({ detail: "Unsupported controls" }, 422));
    vi.stubGlobal("fetch", fetchMock);
    const store = useRealtimeStore();
    store.projectId = "project-a";
    store.controls = structuredClone(controls);

    expect(await store.setDeviceControls("pump", { speed_pct: 72.6 })).toBe(true);
    expect(store.controls.devices.pump.controls.speed_pct.current).toBe(73);
    expect(store.controls.devices.pump.control_source).toBe("manual_override");
    expect(await store.setDeviceControls("pump", { missing: 2 })).toBe(false);
    expect(store.controls.devices.pump.controls.speed_pct.current).toBe(73);
    expect(store.error).toContain("Unsupported controls");
  });

  it("keeps confirmed config on 409 and clears old project runtime data", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response({ detail: { code: "RUNTIME_CONFIG_REQUIRES_STOP" } }, 409)));
    const store = useRealtimeStore();
    store.projectId = "project-a";
    store.config = structuredClone(config);
    store.controls = structuredClone(controls);
    store.rooms = { project_id: "project-a", running: true, simcore_tick: 8, rooms: [] };
    store.pipes = structuredClone(pipes);
    store.rated = structuredClone(rated);

    const changed = { ...config, mode: "heating" as const };
    expect(await store.saveConfig(changed)).toBeNull();
    expect(store.config.mode).toBe("cooling");

    store.disconnect();
    expect(store.projectId).toBeNull();
    expect(store.controls).toBeNull();
    expect(store.rooms).toBeNull();
    expect(store.pipes).toBeNull();
    expect(store.rated).toBeNull();
    expect(store.running).toBe(false);
  });

  it("rejects old ticks and snapshots from a retired session", () => {
    const store = useRealtimeStore();
    store.projectId = "project-a";
    const snapshot = { project_id: "project-a", devices: {}, alarms: [], summary: {}, history_count: 0,
      session_id: "one", simcore_tick: 10, applied_control_revision: 2 };
    expect(store.acceptSnapshot(snapshot)).toBe(true);
    expect(store.acceptSnapshot({ ...snapshot, simcore_tick: 9 })).toBe(false);
    expect(store.acceptSnapshot({ ...snapshot, session_id: "two", simcore_tick: 1 })).toBe(true);
    expect(store.acceptSnapshot({ ...snapshot, simcore_tick: 11 })).toBe(false);
    expect(store.current?.session_id).toBe("two");
    expect(store.acceptControls({ ...controls, session_id: "one", control_revision: 100 })).toBe(false);
  });

  it("does not overwrite a newer control acknowledgement with a delayed read", () => {
    const store = useRealtimeStore();
    store.projectId = "project-a";
    expect(store.acceptControls({ ...controls, session_id: "one", control_revision: 3, simcore_tick: 4 })).toBe(true);
    expect(store.acceptControls({ ...controls, session_id: "one", control_revision: 2, simcore_tick: 5 })).toBe(false);
    expect(store.acceptControls({ ...controls, session_id: "one", control_revision: 3, simcore_tick: 3 })).toBe(false);
    expect(store.controls?.control_revision).toBe(3);
  });

  it("ignores a control response after the user switches projects", async () => {
    let resolve!: (value: Response) => void;
    vi.stubGlobal("fetch", vi.fn().mockImplementation(() => new Promise<Response>((done) => { resolve = done; })));
    const store = useRealtimeStore();
    store.projectId = "project-a";
    const pending = store.setDeviceControls("pump", { speed_pct: 50 });
    store.projectId = "project-b";
    store.clearRuntimeData();
    resolve(response(controls));
    expect(await pending).toBe(false);
    expect(store.controls).toBeNull();
  });
});
