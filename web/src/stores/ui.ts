import { defineStore } from "pinia";

export type ViewName = "building" | "evaluation" | "runtime" | "history" | "catalog" | "diagnostics";

export const useUiStore = defineStore("ui", {
  state: () => ({ view: "building" as ViewName, sidebarOpen: false }),
  actions: {
    navigate(view: ViewName) {
      this.view = view;
      this.sidebarOpen = false;
    },
  },
});
