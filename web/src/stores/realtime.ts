import { defineStore } from "pinia";
import { api, errorMessage } from "@/api/client";
import type {
  LoadState,
  RatedDevicesResult,
  RealtimeControlsResult,
  RealtimePipeStatesResult,
  RealtimeRoomStatesResult,
  RealtimeState,
  SimulationConfig,
} from "@/types";

type SocketState = "disconnected" | "connecting" | "connected" | "reconnecting";

export function defaultSimulationConfig(): SimulationConfig {
  return {
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
}

export function copySimulationConfig(value: SimulationConfig): SimulationConfig {
  return {
    ...value,
    room_initial_conditions: value.room_initial_conditions.map((condition) => ({
      ...condition,
    })),
  };
}

export const useRealtimeStore = defineStore("realtime", {
  state: () => ({
    current: null as RealtimeState | null,
    history: [] as Record<string, unknown>[],
    config: defaultSimulationConfig(),
    controls: null as RealtimeControlsResult | null,
    rooms: null as RealtimeRoomStatesResult | null,
    pipes: null as RealtimePipeStatesResult | null,
    rated: null as RatedDevicesResult | null,
    running: false,
    state: "idle" as LoadState,
    socketState: "disconnected" as SocketState,
    error: "",
    stale: false,
    configSaving: false,
    controlSavingDevice: null as string | null,
    controlsResetting: false,
    socket: null as WebSocket | null,
    projectId: null as string | null,
    reconnectAttempt: 0,
    reconnectTimer: null as ReturnType<typeof setTimeout> | null,
    intentionalClose: false,
    retiredSessions: [] as string[],
  }),
  actions: {
    clearRuntimeData() {
      this.current = null;
      this.history = [];
      this.config = defaultSimulationConfig();
      this.controls = null;
      this.rooms = null;
      this.pipes = null;
      this.rated = null;
      this.running = false;
      this.state = "idle";
      this.error = "";
      this.stale = false;
      this.configSaving = false;
      this.controlSavingDevice = null;
      this.controlsResetting = false;
      this.retiredSessions = [];
    },
    acceptSnapshot(result: RealtimeState): boolean {
      if (result.project_id !== this.projectId) return false;
      const session = result.session_id;
      if (session && this.retiredSessions.includes(session)) return false;
      const previous = this.current;
      if (session && previous?.session_id === session
        && (result.simcore_tick ?? 0) < (previous.simcore_tick ?? 0)) return false;
      if (session && previous?.session_id && session !== previous.session_id) {
        this.retiredSessions.push(previous.session_id);
      }
      this.current = result;
      return true;
    },
    acceptControls(result: RealtimeControlsResult): boolean {
      if (result.project_id !== this.projectId) return false;
      if (result.session_id && this.retiredSessions.includes(result.session_id)) return false;
      if (result.session_id && this.controls?.session_id === result.session_id) {
        if ((result.control_revision ?? 0) < (this.controls.control_revision ?? 0)) return false;
        if (result.control_revision === this.controls.control_revision
          && (result.simcore_tick ?? 0) < (this.controls.simcore_tick ?? 0)) return false;
      }
      this.controls = result;
      return true;
    },
    acceptsProjection(result: { project_id: string; session_id?: string | null; simcore_tick: number }, previous: { session_id?: string | null; simcore_tick: number } | null): boolean {
      if (result.project_id !== this.projectId) return false;
      if (result.session_id && this.retiredSessions.includes(result.session_id)) return false;
      if (result.session_id && previous?.session_id === result.session_id
        && result.simcore_tick < previous.simcore_tick) return false;
      return true;
    },
    async activate(projectId: string) {
      this.disconnect(false);
      this.clearRuntimeData();
      this.projectId = projectId;
      await Promise.all([
        this.loadState(projectId),
        this.loadConfig(projectId),
        this.loadHistory(projectId),
        this.loadControls(projectId),
        this.loadRooms(projectId),
        this.loadPipes(projectId),
        this.loadRated(projectId),
      ]);
      if (this.projectId === projectId) this.connect(projectId);
    },
    async loadState(projectId?: string) {
      const target = projectId ?? this.projectId ?? "";
      if (!target || target === "sample") return;
      this.state = "loading";
      try {
        const result = await api<RealtimeState>(`/api/v1/realtime/state/${encodeURIComponent(target)}`);
        if (this.projectId !== target) return;
        if (!this.acceptSnapshot(result)) return;
        this.stale = this.isStale(result.updated_at);
        this.state = "ready";
      } catch (error) {
        if (this.projectId !== target) return;
        this.error = errorMessage(error);
        this.state = "error";
      }
    },
    async loadHistory(projectId?: string) {
      const target = projectId ?? this.projectId ?? "";
      if (!target || target === "sample") return;
      try {
        const result = await api<{ points: Record<string, unknown>[] }>(`/api/v1/realtime/history/${encodeURIComponent(target)}`);
        if (this.projectId === target) this.history = result.points;
      } catch (error) {
        if (this.projectId === target) this.error = errorMessage(error);
      }
    },
    async loadConfig(projectId?: string) {
      const target = projectId ?? this.projectId ?? "";
      if (!target || target === "sample") return;
      try {
        const result = await api<SimulationConfig>(`/api/v1/realtime/simulate/config/${encodeURIComponent(target)}`);
        if (this.projectId === target) this.config = result;
      } catch (error) {
        if (this.projectId === target) this.error = errorMessage(error);
      }
    },
    async loadControls(projectId?: string) {
      const target = projectId ?? this.projectId ?? "";
      if (!target || target === "sample") return;
      try {
        const result = await api<RealtimeControlsResult>(`/api/v1/realtime/controls/${encodeURIComponent(target)}`);
        if (this.projectId === target) this.acceptControls(result);
      } catch (error) {
        if (this.projectId === target) this.error = errorMessage(error);
      }
    },
    async loadRooms(projectId?: string) {
      const target = projectId ?? this.projectId ?? "";
      if (!target || target === "sample") return;
      try {
        const result = await api<RealtimeRoomStatesResult>(`/api/v1/realtime/rooms/${encodeURIComponent(target)}`);
        if (this.projectId !== target || !this.acceptsProjection(result, this.rooms)) return;
        this.rooms = result;
        this.running = result.running;
      } catch (error) {
        if (this.projectId === target) this.error = errorMessage(error);
      }
    },
    async loadPipes(projectId?: string) {
      const target = projectId ?? this.projectId ?? "";
      if (!target || target === "sample") return;
      try {
        const result = await api<RealtimePipeStatesResult>(`/api/v1/realtime/pipes/${encodeURIComponent(target)}`);
        if (this.projectId === target && this.acceptsProjection(result, this.pipes)) this.pipes = result;
      } catch (error) {
        if (this.projectId === target) this.error = errorMessage(error);
      }
    },
    async loadRated(projectId?: string) {
      const target = projectId ?? this.projectId ?? "";
      if (!target || target === "sample") return;
      try {
        const result = await api<RatedDevicesResult>(`/api/v1/projects/${encodeURIComponent(target)}/devices/rated`);
        if (this.projectId === target) this.rated = result;
      } catch (error) {
        if (this.projectId === target) this.error = errorMessage(error);
      }
    },
    async saveConfig(config: SimulationConfig): Promise<SimulationConfig | null> {
      if (!this.projectId || this.projectId === "sample") return null;
      this.configSaving = true;
      this.error = "";
      try {
        const accepted = await api<SimulationConfig>(`/api/v1/realtime/simulate/config/${encodeURIComponent(this.projectId)}`, {
          method: "POST",
          body: JSON.stringify(config),
        });
        this.config = accepted;
        return accepted;
      } catch (error) {
        this.error = errorMessage(error);
        return null;
      } finally {
        this.configSaving = false;
      }
    },
    async setDeviceControls(deviceId: string, values: Record<string, number>): Promise<boolean> {
      if (!this.projectId || this.projectId === "sample") return false;
      const target = this.projectId;
      this.controlSavingDevice = deviceId;
      this.error = "";
      try {
        const result = await api<RealtimeControlsResult>(`/api/v1/realtime/control/${encodeURIComponent(target)}`, {
          method: "POST",
          body: JSON.stringify({ device_instance_id: deviceId, values }),
        });
        return this.acceptControls(result);
      } catch (error) {
        if (this.projectId === target) this.error = errorMessage(error);
        return false;
      } finally {
        if (this.projectId === target) this.controlSavingDevice = null;
      }
    },
    async resetControls(deviceId?: string): Promise<boolean> {
      if (!this.projectId || this.projectId === "sample") return false;
      const target = this.projectId;
      this.controlsResetting = true;
      this.error = "";
      try {
        const result = await api<RealtimeControlsResult>(`/api/v1/realtime/controls/reset/${encodeURIComponent(target)}`, {
          method: "POST",
          body: JSON.stringify(deviceId ? { device_instance_id: deviceId } : {}),
        });
        return this.acceptControls(result);
      } catch (error) {
        if (this.projectId === target) this.error = errorMessage(error);
        return false;
      } finally {
        if (this.projectId === target) this.controlsResetting = false;
      }
    },
    async syncRuntimeState() {
      if (!this.projectId) return;
      await Promise.all([
        this.loadState(),
        this.loadHistory(),
        this.loadConfig(),
        this.loadControls(),
        this.loadRooms(),
        this.loadPipes(),
      ]);
    },
    async start(config?: SimulationConfig): Promise<boolean> {
      if (!this.projectId || this.projectId === "sample") return false;
      if (!await this.saveConfig(config ?? this.config)) return false;
      try {
        const control = await api<{ running: boolean }>(`/api/v1/realtime/simulate/start/${encodeURIComponent(this.projectId)}`, { method: "POST" });
        this.running = control.running;
        await this.syncRuntimeState();
        return true;
      } catch (error) {
        this.error = errorMessage(error);
        return false;
      }
    },
    async stop(): Promise<boolean> {
      if (!this.projectId || this.projectId === "sample") return false;
      try {
        const control = await api<{ running: boolean }>(`/api/v1/realtime/simulate/stop/${encodeURIComponent(this.projectId)}`, { method: "POST" });
        this.running = control.running;
        await this.syncRuntimeState();
        return true;
      } catch (error) {
        this.error = errorMessage(error);
        return false;
      }
    },
    connect(projectId?: string) {
      const target = projectId ?? this.projectId ?? "";
      if (!target || target === "sample") return;
      this.intentionalClose = false;
      this.socketState = this.reconnectAttempt ? "reconnecting" : "connecting";
      const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
      const socket = new WebSocket(`${protocol}//${window.location.host}/ws/realtime/${encodeURIComponent(target)}`);
      this.socket = socket;
      socket.onopen = () => {
        this.socketState = "connected";
        this.reconnectAttempt = 0;
      };
      socket.onmessage = (event) => {
        if (this.projectId !== target || this.socket !== socket) return;
        const message = JSON.parse(event.data) as { type?: string; data?: unknown };
        if (["connected", "system_tick"].includes(message.type ?? "") && this.isRealtimeState(message.data)) {
          if (!this.acceptSnapshot(message.data)) return;
          this.stale = false;
          if (message.type === "system_tick") void Promise.all([this.loadRooms(target), this.loadPipes(target), this.loadControls(target)]);
        } else if (message.type === "device_state_updated" && this.isRealtimeState(message.data)) {
          // 增量事件只含本次变化的设备：合并进现有快照，不能整体替换。
          const incoming = message.data;
          if (incoming.project_id !== target) return;
          if (this.running && this.current?.session_id) return;
          this.current =
            this.current && this.current.project_id === incoming.project_id
              ? { ...this.current, ...incoming, devices: { ...this.current.devices, ...incoming.devices } }
              : incoming;
          this.stale = false;
        } else if (message.type === "simulation_status" && typeof message.data === "object" && message.data !== null) {
          this.running = (message.data as { running?: boolean }).running === true;
        }
      };
      socket.onerror = () => { this.error = "WebSocket 连接失败"; };
      socket.onclose = () => {
        this.socket = null;
        this.socketState = "disconnected";
        if (!this.intentionalClose && this.projectId === target) this.scheduleReconnect(target);
      };
    },
    disconnect(clearData = true) {
      this.intentionalClose = true;
      if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
      this.socket?.close();
      this.socket = null;
      this.socketState = "disconnected";
      this.reconnectAttempt = 0;
      if (clearData) {
        this.clearRuntimeData();
        this.projectId = null;
      }
    },
    scheduleReconnect(projectId: string) {
      this.reconnectAttempt = Math.min(this.reconnectAttempt + 1, 6);
      const delay = Math.min(1000 * 2 ** (this.reconnectAttempt - 1), 15000);
      this.socketState = "reconnecting";
      this.reconnectTimer = setTimeout(() => this.connect(projectId), delay);
    },
    isRealtimeState(value: unknown): value is RealtimeState {
      return typeof value === "object" && value !== null && "devices" in value && "project_id" in value;
    },
    isStale(value: string | null | undefined): boolean {
      if (!value) return true;
      return Date.now() - new Date(value).getTime() > 15_000;
    },
  },
});
