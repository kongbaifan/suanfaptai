import { defineStore } from "pinia";
import { api, errorMessage } from "@/api/client";
import type { FloorInput, LoadResult, LoadState, ProjectData, RoomInput } from "@/types";

/** 计算延迟：滑块/输入框连续改动时只发最后一次请求。 */
const RECALCULATE_DELAY_MS = 300;

interface Draft {
  floors: FloorInput[];
  rooms: RoomInput[];
  simultaneityFactor: number;
  safetyFactor: number;
}

function cloneDraft(project: ProjectData): Draft {
  return {
    // 浅拷贝每个房间/楼层即可：字段都是标量（`device_ids` 是唯一的数组，单独拷）。
    floors: project.floors.map((floor) => ({ ...floor })),
    rooms: project.rooms.map((room) => ({ ...room, device_ids: [...(room.device_ids ?? [])] })),
    simultaneityFactor: project.simultaneity_factor,
    safetyFactor: project.safety_factor,
  };
}

function blankRoom(floorId: string | null): RoomInput {
  return {
    id: null,
    floor_id: floorId,
    name: "新房间",
    device_ids: [],
    area_m2: 100,
    people: 0,
    purpose: "office",
  };
}

/**
 * 负荷计算草稿。
 *
 * 这是**计算器**，不是项目编辑器：草稿只存在于内存，改完即时算，
 * 不写回项目、不需要先保存。这样课堂上可以放心乱调参数看结果。
 *
 * 权威口径仍然后端：本 store 只负责收集输入并发给
 * `POST /api/v1/load/calculate`，不在这里算任何暖通数值。
 */
export const useLoadStore = defineStore("load", {
  state: () => ({
    floors: [] as FloorInput[],
    rooms: [] as RoomInput[],
    simultaneityFactor: 1,
    safetyFactor: 1,
    result: null as LoadResult | null,
    state: "idle" as LoadState,
    error: "",
    /** 底稿快照，供"恢复项目原值"用。 */
    baseline: null as Draft | null,
    /** 当前底稿的来源标识：同一个来源不重复播种，避免覆盖用户已改的草稿。 */
    seedKey: "",
    // 换项目会让在途请求失效；再加一个自增 revision，让"连续两次改参数"里
    // 先发后到的那次结果被丢弃。
    epoch: 0,
    revision: 0,
    timer: null as ReturnType<typeof setTimeout> | null,
  }),
  getters: {
    /** 全楼是否所有房间在某字段上取同一个值；不一致返回 null。 */
    sharedValue: (state) => (key: string): number | null => {
      if (!state.rooms.length) return null;
      const first = state.rooms[0]?.[key];
      if (typeof first !== "number" || !Number.isFinite(first)) return null;
      return state.rooms.every((room) => room[key] === first) ? first : null;
    },
  },
  actions: {
    /**
     * 以当前项目的房间为底稿。
     *
     * `key` 是来源标识（项目的 `selectionKey`）。同一个 key 重复调用是**空操作**，
     * 否则每次组件挂载都会把用户改了一半的草稿冲掉。
     */
    seedFromProject(project: ProjectData | null, key: string) {
      if (key === this.seedKey) return;
      this.cancelScheduled();
      this.epoch += 1;
      this.revision += 1;
      this.seedKey = key;
      this.result = null;
      this.error = "";
      this.state = "idle";

      if (!project) {
        this.baseline = null;
        this.floors = [];
        this.rooms = [];
        this.simultaneityFactor = 1;
        this.safetyFactor = 1;
        return;
      }
      const draft = cloneDraft(project);
      this.baseline = draft;
      this.floors = draft.floors.map((floor) => ({ ...floor }));
      this.rooms = draft.rooms.map((room) => ({ ...room, device_ids: [...room.device_ids] }));
      this.simultaneityFactor = draft.simultaneityFactor;
      this.safetyFactor = draft.safetyFactor;
    },

    /** 丢弃改动，回到项目原值。 */
    restoreBaseline() {
      if (!this.baseline) return;
      this.cancelScheduled();
      this.epoch += 1;
      this.floors = this.baseline.floors.map((floor) => ({ ...floor }));
      this.rooms = this.baseline.rooms.map((room) => ({ ...room, device_ids: [...room.device_ids] }));
      this.simultaneityFactor = this.baseline.simultaneityFactor;
      this.safetyFactor = this.baseline.safetyFactor;
      void this.recalculate();
    },

    updateRoom(index: number, key: string, value: unknown) {
      const room = this.rooms[index];
      if (!room) return;
      this.rooms[index] = { ...room, [key]: value };
      this.schedule();
    },

    /** 把某个设计参数一次改到全楼所有房间。 */
    applyToAllRooms(key: string, value: unknown) {
      this.rooms = this.rooms.map((room) => ({ ...room, [key]: value }));
      this.schedule();
    },

    setFactor(key: "simultaneityFactor" | "safetyFactor", value: number) {
      this[key] = value;
      this.schedule();
    },

    addRoom() {
      this.rooms = [...this.rooms, blankRoom(this.floors[0]?.id ?? null)];
      this.schedule();
    },

    removeRoom(index: number) {
      this.rooms = this.rooms.filter((_, position) => position !== index);
      this.schedule();
    },

    /** 防抖：连续改参数只发最后一次请求。测试直接调 `recalculate()`。 */
    schedule(delay = RECALCULATE_DELAY_MS) {
      this.cancelScheduled();
      this.timer = setTimeout(() => {
        this.timer = null;
        void this.recalculate();
      }, delay);
    },

    cancelScheduled() {
      if (this.timer !== null) {
        clearTimeout(this.timer);
        this.timer = null;
      }
    },

    async recalculate() {
      if (!this.rooms.length) {
        this.result = null;
        this.state = "idle";
        this.error = "";
        return;
      }
      const epoch = this.epoch;
      const revision = ++this.revision;
      this.state = "loading";
      this.error = "";
      try {
        const result = await api<LoadResult>("/api/v1/load/calculate", {
          method: "POST",
          body: JSON.stringify({
            rooms: this.rooms,
            floors: this.floors,
            simultaneity_factor: this.simultaneityFactor,
            safety_factor: this.safetyFactor,
          }),
        });
        if (epoch !== this.epoch || revision !== this.revision) return;
        this.result = result;
        this.state = "ready";
      } catch (error) {
        if (epoch !== this.epoch || revision !== this.revision) return;
        this.error = errorMessage(error);
        this.state = "error";
      }
    },
  },
});
