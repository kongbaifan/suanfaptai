<script setup lang="ts">
import { computed, onMounted } from "vue";
import { AlertOctagon, Boxes, CheckCircle2, Database, RefreshCw, Server, Wifi } from "lucide-vue-next";
import StatusBadge from "@/components/StatusBadge.vue";
import { useDiagnosticsStore } from "@/stores/diagnostics";
import { useRealtimeStore } from "@/stores/realtime";
import { dateTime, finite } from "@/utils/format";

const diagnostics = useDiagnosticsStore();
const realtime = useRealtimeStore();
const report = computed(() => diagnostics.report);
const service = computed(() => report.value?.service);
const data = computed(() => report.value?.data);
const rt = computed(() => report.value?.realtime);

// 资源检查项名字形如 "resource:app/data/rules.json"，去掉前缀只留可读部分。
function checkLabel(name: string) {
  const separator = name.indexOf(":");
  return separator === -1 ? name : name.slice(separator + 1);
}

function checkGroup(name: string) {
  if (name.startsWith("resource:")) return "资源文件";
  if (name.startsWith("content:")) return "数据条数";
  return "服务";
}

const uptime = computed(() => {
  const seconds = service.value?.uptime_seconds ?? 0;
  if (seconds < 60) return `${finite(seconds, 0)} 秒`;
  if (seconds < 3600) return `${finite(seconds / 60, 1)} 分钟`;
  return `${finite(seconds / 3600, 1)} 小时`;
});

// -1 是后端读取失败的哨兵值，不能当成真实计数展示。
function count(value: number | undefined) {
  return value === undefined || value === -1 ? "读取失败" : String(value);
}

onMounted(() => diagnostics.refresh());
</script>

<template>
  <div class="view-stack">
    <section class="view-heading action-heading">
      <div><p class="eyebrow">DIAGNOSTICS</p><h1>服务自诊断</h1><p>运行环境、发布资源、数据规模与实时仿真状态</p></div>
      <button class="button secondary" @click="diagnostics.refresh"><RefreshCw :class="{ spin: diagnostics.state === 'loading' }" />刷新</button>
    </section>

    <div v-if="diagnostics.error" class="error-banner">诊断接口调用失败：{{ diagnostics.error }}</div>

    <section class="metric-grid diagnostics-metrics">
      <article class="metric-cell"><Server /><span>服务状态</span><strong>{{ report ? (report.status === 'ok' ? '正常' : '异常') : '--' }}</strong><small>{{ service?.app_version ? `v${service.app_version}` : '/api/v1/diagnostics' }}</small></article>
      <article class="metric-cell"><Wifi /><span>WebSocket</span><strong>{{ realtime.socketState }}</strong><small>{{ realtime.projectId || '未选择项目' }}</small></article>
      <article class="metric-cell"><Boxes /><span>实时会话</span><strong>{{ rt?.active_sessions ?? 0 }}</strong><small>{{ rt?.running_simulations ?? 0 }} 个仿真运行中</small></article>
      <article class="metric-cell"><Database /><span>运行时长</span><strong>{{ uptime }}</strong><small>{{ service?.platform || '--' }}</small></article>
    </section>

    <section v-if="diagnostics.failedChecks.length" class="diagnostic-callout">
      <AlertOctagon />
      <div>
        <StatusBadge tone="error" :label="`${diagnostics.failedChecks.length} 项检查未通过`" />
        <h2>发布资源或数据不完整</h2>
        <p>下列检查项失败。标记为必需的项会让服务状态变为异常，参考项仅作提示。</p>
        <dl><template v-for="check in diagnostics.failedChecks" :key="check.name"><dt>{{ checkLabel(check.name) }}</dt><dd>{{ check.detail }}{{ check.required ? '（必需）' : '（参考）' }}</dd></template></dl>
      </div>
    </section>

    <section class="split-band">
      <div class="band-section">
        <div class="section-title"><h2>运行环境</h2></div>
        <div class="kv-grid">
          <div><span>产品</span><strong>{{ service?.product || '--' }}</strong></div>
          <div><span>版本</span><strong>{{ service?.app_version || '--' }}</strong></div>
          <div><span>项目 Schema</span><strong>{{ service?.project_schema_version || '--' }}</strong></div>
          <div><span>Python</span><strong>{{ service?.python_version || '--' }}</strong></div>
          <div><span>平台</span><strong>{{ service?.platform || '--' }}</strong></div>
          <div><span>打包运行</span><strong>{{ service ? (service.frozen ? '是' : '否') : '--' }}</strong></div>
        </div>
      </div>
      <div class="band-section">
        <div class="section-title"><h2>数据规模</h2></div>
        <div class="kv-grid">
          <div><span>设备目录型号</span><strong>{{ count(data?.catalog_items) }}</strong></div>
          <div><span>设备类型</span><strong>{{ count(data?.catalog_types) }}</strong></div>
          <div><span>目录质量问题</span><strong>{{ count(data?.catalog_issues) }}</strong></div>
          <div><span>知识条目</span><strong>{{ count(data?.knowledge_entries) }}</strong></div>
          <div><span>已存项目</span><strong>{{ count(data?.stored_projects) }}</strong></div>
        </div>
      </div>
    </section>

    <section class="band-section">
      <div class="section-title"><div><p class="eyebrow">HEALTH CHECKS</p><h2>检查项</h2></div><span>{{ report?.checks.length ?? 0 }} 项</span></div>
      <div v-if="report?.checks.length" class="log-list">
        <article v-for="check in report.checks" :key="check.name">
          <StatusBadge :tone="check.ok ? 'ok' : check.required ? 'error' : 'warn'" :label="check.ok ? 'OK' : 'FAIL'" />
          <strong>{{ checkLabel(check.name) }}</strong>
          <span>{{ checkGroup(check.name) }}</span>
          <code>{{ check.detail }}</code>
        </article>
      </div>
      <div v-else class="empty-state">尚未获取检查结果。</div>
    </section>

    <section class="band-section">
      <div class="section-title"><div><p class="eyebrow">REALTIME SESSIONS</p><h2>实时仿真会话</h2></div><span>{{ rt?.websocket_subscribers ?? 0 }} 个订阅</span></div>
      <div v-if="rt?.projects.length" class="log-list">
        <article v-for="item in rt.projects" :key="item.project_id">
          <StatusBadge :tone="item.running ? 'ok' : 'muted'" :label="item.running ? '运行中' : '已停止'" />
          <strong>{{ item.project_id }}</strong>
          <span>tick {{ item.tick }} · {{ item.subscribers }} 订阅</span>
          <code>{{ item.updated_at ? dateTime(item.updated_at) : '--' }}</code>
        </article>
      </div>
      <div v-else class="empty-state"><CheckCircle2 />当前没有活动的实时仿真会话。在运行页启动仿真后会出现在这里。</div>
    </section>
  </div>
</template>
