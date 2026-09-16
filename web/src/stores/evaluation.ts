import { defineStore } from "pinia";
import { api, errorMessage } from "@/api/client";
import type {
  EnergyResult,
  EvaluationResult,
  HistoryEntry,
  LoadState,
  ProjectData,
} from "@/types";

export const useEvaluationStore = defineStore("evaluation", {
  state: () => ({
    result: null as EvaluationResult | null,
    energy: null as EnergyResult | null,
    history: [] as HistoryEntry[],
    state: "idle" as LoadState,
    energyState: "idle" as LoadState,
    historyState: "idle" as LoadState,
    evaluationError: "",
    energyError: "",
    historyError: "",
    // A project reset invalidates all requests. Each operation also has its
    // own revision so a simultaneous energy request cannot cancel evaluation.
    epoch: 0,
    evaluationRevision: 0,
    energyRevision: 0,
    historyRevision: 0,
  }),
  getters: {
    // Preserve the existing view contract while letting each operation clear
    // its own failure without hiding an unrelated concurrent failure.
    error: (state): string =>
      [state.evaluationError, state.energyError, state.historyError]
        .filter(Boolean)
        .join("；"),
  },
  actions: {
    reset() {
      this.epoch += 1;
      this.result = null;
      this.energy = null;
      this.history = [];
      this.evaluationError = "";
      this.energyError = "";
      this.historyError = "";
      this.state = "idle";
      this.energyState = "idle";
      this.historyState = "idle";
    },
    async evaluate(projectId: string, project: ProjectData) {
      const epoch = this.epoch;
      const revision = ++this.evaluationRevision;
      this.state = "loading";
      this.evaluationError = "";
      try {
        const result = projectId === "sample"
          ? await api<EvaluationResult>("/api/v1/system/evaluate", {
              method: "POST",
              body: JSON.stringify(project),
            })
          : await api<EvaluationResult>(
              `/api/v1/projects/${encodeURIComponent(projectId)}/evaluate`,
              { method: "POST" },
            );
        if (epoch !== this.epoch || revision !== this.evaluationRevision) return;
        this.result = result;
        this.state = "ready";
        if (projectId !== "sample") await this.loadHistory(projectId);
      } catch (error) {
        if (epoch !== this.epoch || revision !== this.evaluationRevision) return;
        this.evaluationError = errorMessage(error);
        this.state = "error";
      }
    },
    async calculateEnergy(project: ProjectData, hours = 24, price = 0.85) {
      const epoch = this.epoch;
      const revision = ++this.energyRevision;
      this.energyState = "loading";
      this.energyError = "";
      try {
        const result = await api<EnergyResult>("/api/v1/energy/simulate", {
          method: "POST",
          body: JSON.stringify({ project, hours, electricity_price_per_kwh: price }),
        });
        if (epoch !== this.epoch || revision !== this.energyRevision) return;
        this.energy = result;
        this.energyState = "ready";
      } catch (error) {
        if (epoch !== this.epoch || revision !== this.energyRevision) return;
        this.energyError = errorMessage(error);
        this.energyState = "error";
      }
    },
    async loadHistory(projectId: string) {
      const epoch = this.epoch;
      const revision = ++this.historyRevision;
      this.historyError = "";
      if (projectId === "sample") {
        this.history = [];
        this.historyState = "idle";
        return;
      }
      this.historyState = "loading";
      try {
        const result = await api<HistoryEntry[]>(
          `/api/v1/projects/${encodeURIComponent(projectId)}/history`,
        );
        if (epoch !== this.epoch || revision !== this.historyRevision) return;
        this.history = result;
        this.historyState = "ready";
      } catch (error) {
        if (epoch !== this.epoch || revision !== this.historyRevision) return;
        // History refresh is secondary to evaluation. A failed refresh keeps
        // a successful evaluation ready and preserves its result for review.
        this.historyError = errorMessage(error);
        this.historyState = "error";
      }
    },
  },
});
