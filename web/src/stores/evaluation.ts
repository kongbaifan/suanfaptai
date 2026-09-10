import { defineStore } from "pinia";
import { api, errorMessage } from "@/api/client";
import type { EnergyResult, EvaluationResult, HistoryEntry, LoadState, ProjectData } from "@/types";

export const useEvaluationStore = defineStore("evaluation", {
  state: () => ({
    result: null as EvaluationResult | null,
    energy: null as EnergyResult | null,
    history: [] as HistoryEntry[],
    state: "idle" as LoadState,
    error: "",
  }),
  actions: {
    reset() {
      this.result = null;
      this.energy = null;
      this.history = [];
      this.error = "";
      this.state = "idle";
    },
    async evaluate(projectId: string, project: ProjectData) {
      this.state = "loading";
      this.error = "";
      try {
        this.result = projectId === "sample"
          ? await api<EvaluationResult>("/api/v1/system/evaluate", { method: "POST", body: JSON.stringify(project) })
          : await api<EvaluationResult>(`/api/v1/projects/${encodeURIComponent(projectId)}/evaluate`, { method: "POST" });
        this.state = "ready";
        if (projectId !== "sample") await this.loadHistory(projectId);
      } catch (error) {
        this.error = errorMessage(error);
        this.state = "error";
      }
    },
    async calculateEnergy(project: ProjectData, hours = 24, price = 0.85) {
      try {
        this.energy = await api<EnergyResult>("/api/v1/energy/simulate", {
          method: "POST",
          body: JSON.stringify({ project, hours, electricity_price_per_kwh: price }),
        });
      } catch (error) {
        this.error = errorMessage(error);
      }
    },
    async loadHistory(projectId: string) {
      if (projectId === "sample") return;
      try {
        this.history = await api<HistoryEntry[]>(`/api/v1/projects/${encodeURIComponent(projectId)}/history`);
      } catch (error) {
        this.error = errorMessage(error);
      }
    },
  },
});
