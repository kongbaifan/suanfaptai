import { createPinia, setActivePinia } from "pinia";
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  defaultProjectNameForSample,
  useProjectStore,
  type SampleName,
} from "./project";
import { createBlankProjectData } from "@/utils/project-editor";
import type { ProjectData, ProjectRecord } from "@/types";

function response(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const project: ProjectData = {
  floors: [{ id: "F1", name: "一层", level: 1, elevation_m: 0, height_m: 3.6 }],
  rooms: [{ id: "room1", floor_id: "F1", name: "教室", device_ids: ["fcu1"], area_m2: 60, people: 12 }],
  devices: [{ id: "fcu1", type: "风机盘管", quantity: 1 }],
  pipes: [],
  connections: [],
  simultaneity_factor: 0.9,
  safety_factor: 1.1,
  design_options: {},
};

function savedRecord(id: string, name: string): ProjectRecord {
  return {
    project_id: id,
    name,
    description: null,
    tags: ["vue-dashboard"],
    created_at: "2026-09-04T00:00:00Z",
    updated_at: "2026-09-04T00:00:00Z",
    history_count: 0,
    project,
    history: [],
  };
}

function activateSample(store: ReturnType<typeof useProjectStore>, name: SampleName) {
  store.active = {
    ...savedRecord("sample", `${name}（未保存）`),
    project,
  };
  store.usingSample = true;
  store.activeSampleName = name;
}

describe("sample-to-project creation", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    vi.restoreAllMocks();
  });

  it.each([
    ["four_floor_fcu_fresh_air", "四层冷热新风教学项目"],
    ["multi_zone_all_air", "多区域全空气教学项目"],
    ["ducted_fresh_air", "新风机组送回风管教学项目"],
    ["three_floor_twelve_room", "三层十二房间教学项目"],
  ] as const)("uses a stable default name for %s", async (sampleName, defaultName) => {
    const store = useProjectStore();
    activateSample(store, sampleName);
    const created = savedRecord(`created-${sampleName}`, defaultName);
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(response(created))
      .mockResolvedValueOnce(response([{ ...created, history: undefined }]));
    vi.stubGlobal("fetch", fetchMock);

    await expect(store.saveSample("   ")).resolves.toEqual(created);

    const [, init] = fetchMock.mock.calls[0];
    expect(JSON.parse(init.body).name).toBe(defaultName);
    expect(JSON.parse(init.body).project).toEqual(project);
    expect(defaultProjectNameForSample(sampleName)).toBe(defaultName);
    expect(store.active).toEqual(created);
    expect(store.projectId).toBe(created.project_id);
    expect(store.usingSample).toBe(false);
    expect(store.activeSampleName).toBeNull();
    expect(store.projects[0].project_id).toBe(created.project_id);
    expect(store.saving).toBe(false);
  });

  it("posts a custom trimmed name and refreshes the project picker", async () => {
    const store = useProjectStore();
    activateSample(store, "four_floor_fcu_fresh_air");
    const created = savedRecord("custom-id", "我的四层课堂项目");
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(response(created))
      .mockResolvedValueOnce(response([{ ...created, name: created.name }]));
    vi.stubGlobal("fetch", fetchMock);

    await expect(store.saveSample("  我的四层课堂项目  ")).resolves.toEqual(created);

    expect(fetchMock.mock.calls[0][0]).toBe("/api/v1/projects");
    expect(JSON.parse(fetchMock.mock.calls[0][1].body).name).toBe("我的四层课堂项目");
    expect(fetchMock.mock.calls[1][0]).toBe("/api/v1/projects");
    expect(store.projects).toHaveLength(1);
    expect(store.projects[0].name).toBe("我的四层课堂项目");
  });

  it("keeps the created project active when the best-effort list refresh fails", async () => {
    const store = useProjectStore();
    activateSample(store, "four_floor_fcu_fresh_air");
    const created = savedRecord("created-despite-refresh", "四层冷热新风教学项目");
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(response(created))
      .mockRejectedValueOnce(new Error("list unavailable"));
    vi.stubGlobal("fetch", fetchMock);

    await expect(store.saveSample()).resolves.toEqual(created);

    expect(store.active?.project_id).toBe(created.project_id);
    expect(store.usingSample).toBe(false);
    expect(store.projects.some((item) => item.project_id === created.project_id)).toBe(true);
    expect(store.error).toBe("");
    expect(store.saveError).toBe("");
  });

  it("keeps the sample and draft-visible error when creation fails", async () => {
    const store = useProjectStore();
    activateSample(store, "multi_zone_all_air");
    const fetchMock = vi.fn().mockResolvedValue(response({ detail: "项目名称已存在" }, 422));
    vi.stubGlobal("fetch", fetchMock);

    await expect(store.saveSample("重复项目")).resolves.toBeNull();

    expect(store.usingSample).toBe(true);
    expect(store.activeSampleName).toBe("multi_zone_all_air");
    expect(store.projectId).toBe("sample");
    expect(store.saveError).toBe("项目名称已存在");
    expect(store.error).toBe("");
    expect(store.saving).toBe(false);
  });
});

describe("blank project creation", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    vi.restoreAllMocks();
  });

  it("posts the default name with an empty canonical project", async () => {
    const store = useProjectStore();
    activateSample(store, "four_floor_fcu_fresh_air");
    const created = savedRecord("blank-default", "新建教学项目");
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(response(created))
      .mockResolvedValueOnce(response([{ ...created, history: undefined }]));
    vi.stubGlobal("fetch", fetchMock);

    await expect(store.createBlankProject("   ")).resolves.toEqual(created);

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/v1/projects");
    const body = JSON.parse(init.body);
    expect(body.name).toBe("新建教学项目");
    expect(body.project).toEqual(createBlankProjectData());
    expect(body.tags).toEqual(["vue-dashboard"]);
    expect(store.active).toEqual(created);
    expect(store.projectId).toBe(created.project_id);
    expect(store.usingSample).toBe(false);
    expect(store.activeSampleName).toBeNull();
    expect(store.projects[0].project_id).toBe(created.project_id);
    expect(store.saving).toBe(false);
    expect(store.saveError).toBe("");
  });

  it("keeps the previously active project when creation fails", async () => {
    const store = useProjectStore();
    const existing = savedRecord("existing-1", "已有项目");
    store.active = existing;
    store.usingSample = false;
    const fetchMock = vi.fn().mockResolvedValue(response({ detail: "项目名称已存在" }, 422));
    vi.stubGlobal("fetch", fetchMock);

    await expect(store.createBlankProject("重复项目")).resolves.toBeNull();

    expect(store.active?.project_id).toBe("existing-1");
    expect(store.saveError).toBe("项目名称已存在");
    expect(store.error).toBe("");
    expect(store.saving).toBe(false);
  });
});
