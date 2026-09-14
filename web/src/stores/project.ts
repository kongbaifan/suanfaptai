import { defineStore } from "pinia";
import { api, errorMessage } from "@/api/client";
import { createBlankProjectData } from "@/utils/project-editor";
import type { LoadState, ProjectData, ProjectRecord, ProjectSummary } from "@/types";

export const SAMPLE_OPTIONS = [
  {
    name: "complete",
    selectionKey: "sample:complete",
    label: "完整样例（未保存）",
    defaultProjectName: "中央空调教学项目",
  },
  {
    name: "four_floor_fcu_fresh_air",
    selectionKey: "sample:four_floor_fcu_fresh_air",
    label: "四层风机盘管+新风教学样例（未保存）",
    defaultProjectName: "四层冷热新风教学项目",
  },
  {
    name: "multi_zone_all_air",
    selectionKey: "sample:multi_zone_all_air",
    label: "多区域全空气冷热教学样例（未保存）",
    defaultProjectName: "多区域全空气教学项目",
  },
  {
    name: "ducted_fresh_air",
    selectionKey: "sample:ducted_fresh_air",
    label: "新风机组+送回风管教学样例（未保存）",
    defaultProjectName: "新风机组送回风管教学项目",
  },
  {
    name: "three_floor_twelve_room",
    selectionKey: "sample:three_floor_twelve_room",
    label: "三层十二房间教学样例（未保存）",
    defaultProjectName: "三层十二房间教学项目",
  },
] as const;

export type SampleName = (typeof SAMPLE_OPTIONS)[number]["name"];

export function sampleNameForSelection(value: string): SampleName | null {
  return SAMPLE_OPTIONS.find((option) => option.selectionKey === value)?.name ?? null;
}

export function defaultProjectNameForSample(name: SampleName): string {
  return SAMPLE_OPTIONS.find((option) => option.name === name)?.defaultProjectName
    ?? "中央空调教学项目";
}

export const useProjectStore = defineStore("project", {
  state: () => ({
    projects: [] as ProjectSummary[],
    active: null as ProjectRecord | null,
    state: "idle" as LoadState,
    error: "",
    usingSample: false,
    activeSampleName: null as SampleName | null,
    saving: false,
    saveError: "",
  }),
  getters: {
    projectId: (state): string | null => state.active?.project_id ?? null,
    project: (state): ProjectData | null => state.active?.project ?? null,
    sampleDefaultName: (state): string => state.activeSampleName
      ? defaultProjectNameForSample(state.activeSampleName)
      : "中央空调教学项目",
    selectionKey: (state): string | null => state.usingSample && state.activeSampleName
      ? `sample:${state.activeSampleName}`
      : state.active?.project_id ?? null,
  },
  actions: {
    async initialize() {
      await this.loadProjects();
      if (this.projects.length) await this.select(this.projects[0].project_id);
      else await this.loadSample();
    },
    async loadProjects() {
      this.state = "loading";
      this.error = "";
      try {
        this.projects = await api<ProjectSummary[]>("/api/v1/projects");
        this.state = "ready";
      } catch (error) {
        this.error = errorMessage(error);
        this.state = "error";
      }
    },
    async select(projectId: string) {
      this.state = "loading";
      this.error = "";
      try {
        this.active = await api<ProjectRecord>(`/api/v1/projects/${encodeURIComponent(projectId)}`);
        this.usingSample = false;
        this.activeSampleName = null;
        this.saveError = "";
        this.state = "ready";
      } catch (error) {
        this.error = errorMessage(error);
        this.state = "error";
      }
    },
    async loadSample(name: SampleName = "complete") {
      this.state = "loading";
      this.error = "";
      try {
        const project = await api<ProjectData>(`/api/v1/project/sample/${name}`);
        const option = SAMPLE_OPTIONS.find((item) => item.name === name);
        this.active = {
          project_id: "sample",
          name: option?.label ?? `${name}（未保存）`,
          tags: ["sample"],
          created_at: "",
          updated_at: "",
          history_count: 0,
          project,
          history: [],
        };
        this.usingSample = true;
        this.activeSampleName = name;
        this.saveError = "";
        this.state = "ready";
      } catch (error) {
        this.error = errorMessage(error);
        this.state = "error";
      }
    },
    /** 整份项目回写。失败不写 `error`：那个字段会被 App.vue 渲染成"项目加载失败"，
     *  而这里的失败是保存失败，尤其 409（仿真运行中）需要独立提示。 */
    async updateProject(project: ProjectData): Promise<boolean> {
      if (!this.active || this.usingSample) return false;
      this.saving = true;
      this.saveError = "";
      try {
        const saved = await api<ProjectRecord>(
          `/api/v1/projects/${encodeURIComponent(this.active.project_id)}`,
          {
            method: "PUT",
            body: JSON.stringify({
              name: this.active.name,
              description: this.active.description ?? null,
              tags: this.active.tags,
              project,
            }),
          },
        );
        this.active = saved;
        await this.loadProjects();
        return true;
      } catch (error) {
        this.saveError = errorMessage(error);
        return false;
      } finally {
        this.saving = false;
      }
    },
    async saveSample(name?: string): Promise<ProjectRecord | null> {
      if (!this.active || !this.usingSample) return null;
      this.saving = true;
      this.error = "";
      this.saveError = "";
      try {
        const sampleName = this.activeSampleName;
        const requestedName = name?.trim();
        const projectName = requestedName || (sampleName ? defaultProjectNameForSample(sampleName) : "中央空调教学项目");
        const saved = await api<ProjectRecord>("/api/v1/projects", {
          method: "POST",
          body: JSON.stringify({ name: projectName, project: this.active.project, tags: ["vue-dashboard"] }),
        });
        await this.activateCreatedProject(saved);
        return saved;
      } catch (error) {
        // Keep the sample active and expose save failures separately from the
        // global load error so the learner can retry without losing the draft.
        this.saveError = errorMessage(error);
        this.state = "ready";
        return null;
      } finally {
        this.saving = false;
      }
    },
    /** 不依赖样例、从零新建空白教学项目；成功后直达可编辑状态。 */
    async createBlankProject(name?: string): Promise<ProjectRecord | null> {
      this.saving = true;
      this.error = "";
      this.saveError = "";
      try {
        const projectName = name?.trim() || "新建教学项目";
        const saved = await api<ProjectRecord>("/api/v1/projects", {
          method: "POST",
          body: JSON.stringify({ name: projectName, project: createBlankProjectData(), tags: ["vue-dashboard"] }),
        });
        await this.activateCreatedProject(saved);
        return saved;
      } catch (error) {
        // 失败保持当前项目/样例不变，saveError 独立提示，便于重试。
        this.saveError = errorMessage(error);
        this.state = "ready";
        return null;
      } finally {
        this.saving = false;
      }
    },
    /** POST /api/v1/projects 成功后的共同收尾：以服务端返回为准刷新选择器并激活。 */
    async activateCreatedProject(saved: ProjectRecord) {
      await this.loadProjects()
      // A successful POST is authoritative even if a concurrent list refresh
      // briefly returns stale data. Keep the returned record in the picker.
      if (!this.projects.some((item) => item.project_id === saved.project_id)) {
        this.projects = [
          {
            project_id: saved.project_id,
            name: saved.name,
            description: saved.description,
            tags: saved.tags,
            created_at: saved.created_at,
            updated_at: saved.updated_at,
            history_count: saved.history_count,
          },
          ...this.projects,
        ];
      }
      this.active = saved;
      this.usingSample = false;
      this.activeSampleName = null;
      // A successful POST is authoritative.  A failed best-effort list
      // refresh must not leave a stale global "project load failed" banner
      // over the newly activated project.
      this.error = "";
      this.state = "ready";
    },
    /** 重命名项目：走 PUT（保留 project 内容），成功后刷新列表。 */
    async rename(projectId: string, name: string): Promise<boolean> {
      const trimmed = name.trim();
      if (!trimmed) return false;
      this.saving = true;
      this.saveError = "";
      try {
        const current = this.active?.project_id === projectId
          ? this.active
          : await api<ProjectRecord>(`/api/v1/projects/${encodeURIComponent(projectId)}`);
        const saved = await api<ProjectRecord>(
          `/api/v1/projects/${encodeURIComponent(projectId)}`,
          {
            method: "PUT",
            body: JSON.stringify({
              name: trimmed,
              description: current.description ?? null,
              tags: current.tags,
              project: current.project,
            }),
          },
        );
        if (this.active?.project_id === projectId) this.active = saved;
        await this.loadProjects();
        return true;
      } catch (error) {
        this.saveError = errorMessage(error);
        return false;
      } finally {
        this.saving = false;
      }
    },
    /** 复制项目为副本，并把新副本设为当前项目。 */
    async duplicate(projectId: string): Promise<ProjectRecord | null> {
      this.saving = true;
      this.saveError = "";
      try {
        const copy = await api<ProjectRecord>(
          `/api/v1/projects/${encodeURIComponent(projectId)}/duplicate`,
          { method: "POST" },
        );
        await this.activateCreatedProject(copy);
        return copy;
      } catch (error) {
        this.saveError = errorMessage(error);
        return null;
      } finally {
        this.saving = false;
      }
    },
    /** 删除项目。若删的是当前项目，则回退到列表首个项目或样例。 */
    async remove(projectId: string): Promise<boolean> {
      this.saving = true;
      this.saveError = "";
      try {
        await api<{ deleted: boolean }>(
          `/api/v1/projects/${encodeURIComponent(projectId)}`,
          { method: "DELETE" },
        );
        const wasActive = this.active?.project_id === projectId;
        await this.loadProjects();
        if (wasActive) {
          if (this.projects.length) await this.select(this.projects[0].project_id);
          else await this.loadSample();
        }
        return true;
      } catch (error) {
        this.saveError = errorMessage(error);
        return false;
      } finally {
        this.saving = false;
      }
    },
  },
});
