import { defineStore } from "pinia";

export type ViewName = "overview" | "building" | "evaluation" | "runtime" | "history" | "resources" | "catalog" | "diagnostics";

export const useUiStore = defineStore("ui", {
  state: () => ({ view: "overview" as ViewName, sidebarOpen: false, buildingDirty: false }),
  actions: {
    navigate(view: ViewName) {
      this.view = view;
      this.sidebarOpen = false;
    },
  },
});
