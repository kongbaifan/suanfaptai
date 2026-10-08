import { defineStore } from "pinia";
import { markRaw } from "vue";
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
type RuntimeContext = { projectId: string; revision: number };

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
    contextRevision: 0,
  }),
  actions: {
    captureContext(projectId?: string): RuntimeContext | null {
      const target = projectId ?? this.projectId;
      if (!target || target === "sample" || target !== this.projectId) return null;
      return { projectId: target, revision: this.contextRevision };
    },
    isCurrentContext(context: RuntimeContext): boolean {
      return context.projectId === this.projectId && context.revision === this.contextRevision;
    },
    clearRuntimeData() {
      // A -> B -> A is a new context even though the final project ID matches.
      this.contextRevision += 1;
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
      const context = this.captureContext(projectId);
      if (!context) return;
      await Promise.all([
        this.loadState(projectId),
        this.loadConfig(projectId),
        this.loadHistory(projectId),
        this.loadControls(projectId),
        this.loadRooms(projectId),
        this.loadPipes(projectId),
        this.loadRated(projectId),
      ]);
      if (this.isCurrentContext(context)) this.connect(projectId);
    },
    async loadState(projectId?: string) {
      const context = this.captureContext(projectId);
      if (!context) return;
      const target = context.projectId;
      this.state = "loading";
      try {
        const result = await api<RealtimeState>(`/api/v1/realtime/state/${encodeURIComponent(target)}`);
        if (!this.isCurrentContext(context)) return;
        if (!this.acceptSnapshot(result)) return;
        this.stale = this.isStale(result.updated_at);
        this.state = "ready";
      } catch (error) {
        if (!this.isCurrentContext(context)) return;
        this.error = errorMessage(error);
        this.state = "error";
      }
    },
    async loadHistory(projectId?: string) {
      const context = this.captureContext(projectId);
      if (!context) return;
      const target = context.projectId;
      try {
        const result = await api<{ points: Record<string, unknown>[] }>(`/api/v1/realtime/history/${encodeURIComponent(target)}`);
        if (this.isCurrentContext(context)) this.history = result.points;
      } catch (error) {
        if (this.isCurrentContext(context)) this.error = errorMessage(error);
      }
    },
    async loadConfig(projectId?: string) {
      const context = this.captureContext(projectId);
      if (!context) return;
      const target = context.projectId;
      try {
        const result = await api<SimulationConfig>(`/api/v1/realtime/simulate/config/${encodeURIComponent(target)}`);
        if (this.isCurrentContext(context)) this.config = result;
      } catch (error) {
        if (this.isCurrentContext(context)) this.error = errorMessage(error);
      }
    },
    async loadControls(projectId?: string) {
      const context = this.captureContext(projectId);
      if (!context) return;
      const target = context.projectId;
      try {
        const result = await api<RealtimeControlsResult>(`/api/v1/realtime/controls/${encodeURIComponent(target)}`);
        if (this.isCurrentContext(context)) this.acceptControls(result);
      } catch (error) {
        if (this.isCurrentContext(context)) this.error = errorMessage(error);
      }
    },
    async loadRooms(projectId?: string) {
      const context = this.captureContext(projectId);
      if (!context) return;
      const target = context.projectId;
      try {
        const result = await api<RealtimeRoomStatesResult>(`/api/v1/realtime/rooms/${encodeURIComponent(target)}`);
        if (!this.isCurrentContext(context) || !this.acceptsProjection(result, this.rooms)) return;
        this.rooms = result;
        this.running = result.running;
      } catch (error) {
        if (this.isCurrentContext(context)) this.error = errorMessage(error);
      }
    },
    async loadPipes(projectId?: string) {
      const context = this.captureContext(projectId);
      if (!context) return;
      const target = context.projectId;
      try {
        const result = await api<RealtimePipeStatesResult>(`/api/v1/realtime/pipes/${encodeURIComponent(target)}`);
        if (this.isCurrentContext(context) && this.acceptsProjection(result, this.pipes)) this.pipes = result;
      } catch (error) {
        if (this.isCurrentContext(context)) this.error = errorMessage(error);
      }
    },
    async loadRated(projectId?: string) {
      const context = this.captureContext(projectId);
      if (!context) return;
      const target = context.projectId;
      try {
        const result = await api<RatedDevicesResult>(`/api/v1/projects/${encodeURIComponent(target)}/devices/rated`);
        if (this.isCurrentContext(context)) this.rated = result;
      } catch (error) {
        if (this.isCurrentContext(context)) this.error = errorMessage(error);
      }
    },
    async saveConfig(config: SimulationConfig): Promise<SimulationConfig | null> {
      const context = this.captureContext();
      if (!context) return null;
      this.configSaving = true;
      this.error = "";
      try {
        const accepted = await api<SimulationConfig>(`/api/v1/realtime/simulate/config/${encodeURIComponent(context.projectId)}`, {
          method: "POST",
          body: JSON.stringify(config),
        });
        if (!this.isCurrentContext(context)) return null;
        this.config = accepted;
        return accepted;
      } catch (error) {
        if (this.isCurrentContext(context)) this.error = errorMessage(error);
        return null;
      } finally {
        if (this.isCurrentContext(context)) this.configSaving = false;
      }
    },
    async setDeviceControls(deviceId: string, values: Record<string, number>): Promise<boolean> {
      const context = this.captureContext();
      if (!context) return false;
      const target = context.projectId;
      this.controlSavingDevice = deviceId;
      this.error = "";
      try {
        const result = await api<RealtimeControlsResult>(`/api/v1/realtime/control/${encodeURIComponent(target)}`, {
          method: "POST",
          body: JSON.stringify({ device_instance_id: deviceId, values }),
        });
        return this.isCurrentContext(context) && this.acceptControls(result);
      } catch (error) {
        if (this.isCurrentContext(context)) this.error = errorMessage(error);
        return false;
      } finally {
        if (this.isCurrentContext(context)) this.controlSavingDevice = null;
      }
    },
    async resetControls(deviceId?: string): Promise<boolean> {
      const context = this.captureContext();
      if (!context) return false;
      const target = context.projectId;
      this.controlsResetting = true;
      this.error = "";
      try {
        const result = await api<RealtimeControlsResult>(`/api/v1/realtime/controls/reset/${encodeURIComponent(target)}`, {
          method: "POST",
          body: JSON.stringify(deviceId ? { device_instance_id: deviceId } : {}),
        });
        return this.isCurrentContext(context) && this.acceptControls(result);
      } catch (error) {
        if (this.isCurrentContext(context)) this.error = errorMessage(error);
        return false;
      } finally {
        if (this.isCurrentContext(context)) this.controlsResetting = false;
      }
    },
    async syncRuntimeState() {
      const context = this.captureContext();
      if (!context || !this.isCurrentContext(context)) return;
      await Promise.all([
        this.loadState(context.projectId),
        this.loadHistory(context.projectId),
        this.loadConfig(context.projectId),
        this.loadControls(context.projectId),
        this.loadRooms(context.projectId),
        this.loadPipes(context.projectId),
      ]);
    },
    async start(config?: SimulationConfig): Promise<boolean> {
      const context = this.captureContext();
      if (!context) return false;
      if (!await this.saveConfig(config ?? this.config)) return false;
      if (!this.isCurrentContext(context)) return false;
      try {
        const control = await api<{ running: boolean }>(`/api/v1/realtime/simulate/start/${encodeURIComponent(context.projectId)}`, { method: "POST" });
        if (!this.isCurrentContext(context)) return false;
        this.running = control.running;
        await this.syncRuntimeState();
        return this.isCurrentContext(context);
      } catch (error) {
        if (this.isCurrentContext(context)) this.error = errorMessage(error);
        return false;
      }
    },
    async stop(): Promise<boolean> {
      const context = this.captureContext();
      if (!context) return false;
      try {
        const control = await api<{ running: boolean }>(`/api/v1/realtime/simulate/stop/${encodeURIComponent(context.projectId)}`, { method: "POST" });
        if (!this.isCurrentContext(context)) return false;
        this.running = control.running;
        await this.syncRuntimeState();
        return this.isCurrentContext(context);
      } catch (error) {
        if (this.isCurrentContext(context)) this.error = errorMessage(error);
        return false;
      }
    },
    connect(projectId?: string) {
      const context = this.captureContext(projectId);
      if (!context) return;
      const target = context.projectId;
      if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
      const previous = this.socket;
      this.socket = null;
      previous?.close();
      this.intentionalClose = false;
      this.socketState = this.reconnectAttempt ? "reconnecting" : "connecting";
      const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
      const socket = markRaw(new WebSocket(`${protocol}//${window.location.host}/ws/realtime/${encodeURIComponent(target)}`));
      this.socket = socket;
      const isCurrentSocket = () => this.isCurrentContext(context) && this.socket === socket;
      socket.onopen = () => {
        if (!isCurrentSocket()) return;
        this.socketState = "connected";
        this.reconnectAttempt = 0;
      };
      socket.onmessage = (event) => {
        if (!isCurrentSocket()) return;
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
      socket.onerror = () => { if (isCurrentSocket()) this.error = "WebSocket 连接失败"; };
      socket.onclose = () => {
        if (!isCurrentSocket()) return;
        this.socket = null;
        this.socketState = "disconnected";
        if (!this.intentionalClose && this.projectId === target) this.scheduleReconnect(target);
      };
    },
    disconnect(clearData = true) {
      this.contextRevision += 1;
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
      const context = this.captureContext(projectId);
      if (!context) return;
      this.reconnectAttempt = Math.min(this.reconnectAttempt + 1, 6);
      const delay = Math.min(1000 * 2 ** (this.reconnectAttempt - 1), 15000);
      this.socketState = "reconnecting";
      this.reconnectTimer = setTimeout(() => {
        if (this.isCurrentContext(context)) this.connect(projectId);
      }, delay);
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
