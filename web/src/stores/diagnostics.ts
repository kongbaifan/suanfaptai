import { defineStore } from "pinia";
import { api, errorMessage } from "@/api/client";
import type { Diagnostics, LoadState } from "@/types";

export const useDiagnosticsStore = defineStore("diagnostics", {
  state: () => ({
    report: null as Diagnostics | null,
    state: "idle" as LoadState,
    error: "",
  }),
  getters: {
    failedChecks: (state) => (state.report?.checks ?? []).filter((check) => !check.ok),
    // 只有 required 项失败才算服务异常，参考项失败仅提示。
    healthy: (state) =>
      state.report !== null &&
      state.report.status === "ok" &&
      state.error === "",
  },
  actions: {
    async refresh() {
      this.state = "loading";
      this.error = "";
      try {
        this.report = await api<Diagnostics>("/api/v1/diagnostics");
        this.state = "ready";
      } catch (error) {
        this.error = errorMessage(error);
        this.state = "error";
      }
    },
  },
});
