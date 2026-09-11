<script setup lang="ts">
/**
 * 项目管理面板：替代顶栏那个只能选择、无法维护的下拉框。
 * 提供搜索、重命名、复制、删除，以及从样例/空白新建。
 */
import { computed, ref } from "vue";
import { Copy, Pencil, Plus, Search, Trash2, X } from "lucide-vue-next";
import { SAMPLE_OPTIONS, sampleNameForSelection, useProjectStore } from "@/stores/project";

const emit = defineEmits<{ (e: "close"): void; (e: "created"): void }>();

const project = useProjectStore();
const query = ref("");
const renamingId = ref<string | null>(null);
const renameDraft = ref("");
const newName = ref("");
const notice = ref("");

const filtered = computed(() => {
  const q = query.value.trim().toLowerCase();
  if (!q) return project.projects;
  return project.projects.filter((item) =>
    `${item.name} ${item.description ?? ""} ${item.tags.join(" ")}`.toLowerCase().includes(q),
  );
});

const activeId = computed(() =>
  project.usingSample ? null : project.projectId,
);

function formatTime(value: string): string {
  if (!value) return "--";
  return new Date(value).toLocaleString("zh-CN", { hour12: false });
}

async function open(projectId: string) {
  await project.select(projectId);
  emit("close");
}

async function openSample(selectionKey: string) {
  const sampleName = sampleNameForSelection(selectionKey);
  if (sampleName) await project.loadSample(sampleName);
  emit("close");
}

function startRename(projectId: string, currentName: string) {
  renamingId.value = projectId;
  renameDraft.value = currentName;
}

async function commitRename() {
  if (!renamingId.value) return;
  const ok = await project.rename(renamingId.value, renameDraft.value);
  if (ok) {
    notice.value = "已重命名。";
    renamingId.value = null;
  }
}

async function duplicate(projectId: string) {
  const copy = await project.duplicate(projectId);
  if (copy) {
    notice.value = `已复制为「${copy.name}」并切换过去。`;
    emit("created");
  }
}

async function remove(projectId: string, name: string) {
  if (!window.confirm(`确定删除项目「${name}」？该操作不可撤销，评估历史一并删除。`)) return;
  const ok = await project.remove(projectId);
  if (ok) notice.value = "已删除。";
}

async function createBlank() {
  const saved = await project.createBlankProject(newName.value);
  if (saved) {
    newName.value = "";
    notice.value = `已新建「${saved.name}」。`;
    emit("created");
  }
}
</script>

<template>
  <div class="pm-backdrop" @click.self="emit('close')">
    <section class="pm-panel" role="dialog" aria-label="项目管理">
      <header class="pm-head">
        <div><h2>项目管理</h2><p>共 {{ project.projects.length }} 个项目</p></div>
        <button class="icon-button" type="button" title="关闭" @click="emit('close')"><X /></button>
      </header>

      <div v-if="notice" class="pm-notice">{{ notice }}</div>
      <div v-if="project.saveError" class="pm-error">{{ project.saveError }}</div>

      <label class="pm-search"><Search /><input v-model="query" type="search" placeholder="搜索项目名称、描述或标签" aria-label="搜索项目" /></label>

      <div class="pm-list">
        <p v-if="filtered.length === 0" class="pm-empty">没有匹配的项目。</p>
        <article v-for="item in filtered" :key="item.project_id" class="pm-item" :class="{ active: item.project_id === activeId }">
          <div class="pm-item-main">
            <template v-if="renamingId === item.project_id">
              <input
                v-model="renameDraft"
                class="pm-rename-input"
                aria-label="项目名称"
                @keyup.enter="commitRename"
                @keyup.esc="renamingId = null"
              />
              <button class="button secondary" type="button" @click="commitRename">保存</button>
              <button class="button secondary" type="button" @click="renamingId = null">取消</button>
            </template>
            <template v-else>
              <button class="pm-name" type="button" @click="open(item.project_id)">
                <strong>{{ item.name }}</strong>
                <small>{{ formatTime(item.updated_at) }} · 评估 {{ item.history_count }} 次</small>
              </button>
              <div class="pm-actions">
                <button class="icon-button" type="button" title="重命名" @click="startRename(item.project_id, item.name)"><Pencil /></button>
                <button class="icon-button" type="button" title="复制为副本" @click="duplicate(item.project_id)"><Copy /></button>
                <button class="icon-button danger" type="button" title="删除项目" @click="remove(item.project_id, item.name)"><Trash2 /></button>
              </div>
            </template>
          </div>
          <p v-if="item.description" class="pm-desc">{{ item.description }}</p>
        </article>
      </div>

      <footer class="pm-foot">
        <div class="pm-new">
          <input v-model="newName" class="pm-rename-input" aria-label="新项目名称" placeholder="新建教学项目" @keyup.enter="createBlank" />
          <button class="button secondary" type="button" :disabled="project.saving" @click="createBlank"><Plus />新建空白项目</button>
        </div>
        <div class="pm-samples">
          <span>从教学样例开始：</span>
          <button v-for="sample in SAMPLE_OPTIONS" :key="sample.selectionKey" class="button secondary" type="button" @click="openSample(sample.selectionKey)">{{ sample.label.replace("（未保存）", "") }}</button>
        </div>
      </footer>
    </section>
  </div>
</template>

<style scoped>
.pm-backdrop { position: fixed; inset: 0; z-index: 60; display: grid; place-items: center; padding: 24px; background: rgba(10, 30, 33, .55); }
.pm-panel { width: min(680px, 94vw); max-height: 86vh; display: flex; flex-direction: column; background: #fff; border-radius: 8px; box-shadow: 0 24px 60px rgba(0, 0, 0, .28); overflow: hidden; }
.pm-head { display: flex; align-items: center; justify-content: space-between; padding: 16px 18px; background: var(--teal-dark); color: #fff; }
.pm-head h2 { margin: 0; font-size: 16px; }
.pm-head p { margin: 2px 0 0; font-size: 12px; opacity: .8; }
.pm-head .icon-button { background: transparent; border-color: #52757a; color: #fff; }
.pm-notice { padding: 10px 18px; font-size: 13px; color: #326a54; background: #edf6f1; border-bottom: 1px solid #b6d7c8; }
.pm-error { padding: 10px 18px; font-size: 13px; color: #88322e; background: #fae7e5; border-bottom: 1px solid #e5b6b2; }
.pm-search { display: flex; align-items: center; gap: 8px; margin: 14px 18px 6px; padding: 0 12px; height: 38px; border: 1px solid #c7d1cf; border-radius: 4px; }
.pm-search input { flex: 1; border: 0; outline: 0; font-size: 13px; }
.pm-list { flex: 1; overflow: auto; padding: 6px 18px 12px; }
.pm-empty { padding: 24px 0; color: var(--muted); font-size: 13px; text-align: center; }
.pm-item { padding: 10px 0; border-bottom: 1px solid #eef2f1; }
.pm-item.active { box-shadow: inset 3px 0 0 var(--teal-dark); padding-left: 10px; }
.pm-item-main { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
.pm-name { flex: 1; display: grid; gap: 2px; text-align: left; background: none; border: 0; cursor: pointer; padding: 2px 0; }
.pm-name strong { font-size: 14px; color: #1d3b3f; }
.pm-name small { font-size: 12px; color: var(--muted); }
.pm-actions { display: flex; gap: 6px; }
.pm-actions .icon-button { width: 30px; height: 30px; }
.pm-desc { margin: 4px 0 0; font-size: 12px; color: var(--muted); }
.pm-rename-input { height: 34px; padding: 0 10px; font-size: 13px; border: 1px solid #c7d1cf; border-radius: 4px; }
.pm-foot { padding: 14px 18px; background: #f5f7f6; border-top: 1px solid #e3e9e7; display: grid; gap: 10px; }
.pm-new { display: flex; gap: 8px; align-items: center; }
.pm-new .pm-rename-input { flex: 1; }
.pm-samples { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; font-size: 12px; color: var(--muted); }
.pm-samples .button { font-size: 12px; padding: 6px 10px; }
@media (max-width: 720px) {
  .pm-item-main { flex-direction: column; align-items: stretch; }
  .pm-actions { justify-content: flex-end; }
}
</style>
