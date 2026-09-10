import { createPinia, setActivePinia } from "pinia";
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  SAMPLE_OPTIONS,
  sampleNameForSelection,
  useProjectStore,
} from "./project";
import type { ProjectData, ProjectRecord } from "@/types";


const sampleProject: ProjectData = {
  floors: [],
  rooms: [],
  devices: [],
  pipes: [],
  connections: [],
  simultaneity_factor: 1,
  safety_factor: 1,
  design_options: {},
};

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function savedRecord(rooms: ProjectData["rooms"]): ProjectRecord {
  return {
    project_id: "p1",
    name: "已保存项目",
    description: null,
    tags: ["vue-dashboard"],
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-02T00:00:00Z",
    history_count: 0,
    project: { ...sampleProject, rooms },
    history: [],
  };
}


describe("built-in project samples", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    vi.restoreAllMocks();
  });

  it("owns the selector keys and labels in one typed list", () => {
    expect(SAMPLE_OPTIONS.map((item) => item.selectionKey)).toEqual([
      "sample:complete",
      "sample:four_floor_fcu_fresh_air",
      "sample:multi_zone_all_air",
    ]);
    expect(sampleNameForSelection("sample:four_floor_fcu_fresh_air")).toBe(
      "four_floor_fcu_fresh_air",
    );
    expect(sampleNameForSelection("saved-project")).toBeNull();
    expect(sampleNameForSelection("sample:multi_zone_all_air")).toBe(
      "multi_zone_all_air",
    );
  });

  it("loads the teaching sample while retaining the unsaved sample project id", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify(sampleProject), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const store = useProjectStore();

    await store.loadSample("four_floor_fcu_fresh_air");

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/project/sample/four_floor_fcu_fresh_air",
      expect.any(Object),
    );
    expect(store.projectId).toBe("sample");
    expect(store.selectionKey).toBe("sample:four_floor_fcu_fresh_air");
    expect(store.usingSample).toBe(true);
    expect(store.active?.name).toBe("四层风机盘管+新风教学样例（未保存）");
  });

  it("loads the multi-zone all-air sample from the typed selector", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify(sampleProject), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const store = useProjectStore();

    await store.loadSample("multi_zone_all_air");

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/project/sample/multi_zone_all_air",
      expect.any(Object),
    );
    expect(store.selectionKey).toBe("sample:multi_zone_all_air");
    expect(store.active?.name).toBe("多区域全空气冷热教学样例（未保存）");
  });
});

describe("project update", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    vi.restoreAllMocks();
  });

  it("puts the edited project and keeps name, tags and description", async () => {
    const store = useProjectStore();
    store.active = savedRecord([{ name: "r1", device_ids: [], area_m2: 100, people: 4 }]);
    store.usingSample = false;
    const edited: ProjectData = {
      ...sampleProject,
      rooms: [{ name: "r1", device_ids: [], area_m2: 155.5, people: 4 }],
    };
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(json(savedRecord(edited.rooms)))
      .mockResolvedValueOnce(json([]));
    vi.stubGlobal("fetch", fetchMock);

    await expect(store.updateProject(edited)).resolves.toBe(true);

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/v1/projects/p1");
    expect(init.method).toBe("PUT");
    expect(JSON.parse(init.body)).toEqual({
      name: "已保存项目",
      description: null,
      tags: ["vue-dashboard"],
      project: edited,
    });
    expect(store.active?.project.rooms[0].area_m2).toBe(155.5);
    expect(store.saveError).toBe("");
    expect(store.saving).toBe(false);
  });

  it("refuses to put a sample project", async () => {
    const store = useProjectStore();
    store.active = savedRecord([]);
    store.usingSample = true;
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);

    await expect(store.updateProject(sampleProject)).resolves.toBe(false);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("reports the 409 message on saveError and leaves the global error untouched", async () => {
    const store = useProjectStore();
    store.active = savedRecord([]);
    store.usingSample = false;
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(json({
      detail: { code: "PROJECT_EDIT_REQUIRES_STOP", message: "Stop the simulation before editing the project." },
    }, 409)));

    await expect(store.updateProject(sampleProject)).resolves.toBe(false);

    expect(store.saveError).toBe("Stop the simulation before editing the project.");
    expect(store.error).toBe("");
    expect(store.saving).toBe(false);
  });
});
