<template>
  <section class="page" :data-module="meta.key">
    <header class="page-head">
      <div>
        <h2>{{ meta.name }}</h2>
        <p class="page-desc">{{ meta.desc }}</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">
          登记{{ meta.entity }}
        </button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">
          导出{{ meta.name }}清单
        </button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <p class="status-legend">
      <span v-for="item in statusSummary" :key="item.status" class="legend-item">
        {{ item.status }}：{{ item.count }}
      </span>
    </p>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :data-testid="`filter-${field}`" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" data-testid="query" type="submit">查询</button>
      <button class="btn ghost" data-testid="reset" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in meta.fields" :key="column">{{ column }}</th>
          <th v-if="supportsTrace">正文 / 重算来源</th>
          <th>当前状态</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :data-testid="`row-${row.id}`" :data-status="String(row.status)">
          <td v-for="column in meta.fields" :key="column">{{ row[column] ?? '—' }}</td>
          <td v-if="supportsTrace" class="trace-cell" data-testid="trace-cell">
            <div v-if="row['修订编号'] || row['_revision_id'] || row.revision_id" class="trace-lines">
              <span>修订：{{ row['修订编号'] || row['_revision_id'] || row.revision_id }}</span>
              <span v-if="row['重算编号']">重算：{{ row['重算编号'] }}</span>
              <span v-if="row['来源纠错单']">来源：{{ row['来源纠错单'] }}</span>
              <pre v-if="row['正文'] || row.content || row.revision?.content" class="inline-content">{{ row['正文'] || row.content || row.revision?.content }}</pre>
            </div>
            <span v-else class="muted">尚未查到同一次修订的重算结果</span>
            <button class="link" type="button" :data-testid="`trace-button-${row.id}`" @click="openDetail(row)">
              修订详情
            </button>
          </td>
          <td>{{ row.status }}</td>
          <td class="row-actions">
            <button
              v-for="action in meta.actions"
              :key="action"
              class="link"
              type="button"
              :data-testid="`action-${row.id}-${action}`"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
            <button
              v-if="supportsTrace"
              class="link"
              type="button"
              :data-testid="`download-button-${row.id}`"
              @click="downloadRow(row)"
            >
              下载字幕
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="meta.fields.length + (supportsTrace ? 3 : 2)" class="empty-state">
            暂无{{ meta.name }}数据，可先登记{{ meta.entity }}
          </td>
        </tr>
      </tbody>
    </table>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记{{ meta.entity }}</h3>
        <label v-for="field in meta.fields" :key="field" class="create-field">
          <span>{{ field }}</span>
          <input v-model="createValues[field]" :data-testid="`create-${field}`" required />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="showCreate = false">取消</button>
          <button class="btn primary" data-testid="submit-create" type="submit" :disabled="creating">
            {{ creating ? '提交中…' : '确认登记' }}
          </button>
        </div>
      </form>
    </div>

    <footer class="page-foot">
      <span>共 {{ total }} 条{{ meta.name }}记录</span>
      <span v-if="successMessage" class="success-text" data-testid="success-message">{{ successMessage }}</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>

    <div v-if="detailRow" class="modal-backdrop" data-testid="revision-detail-backdrop" @click.self="closeDetail">
      <section class="modal revision-modal" data-testid="revision-detail" role="dialog" aria-modal="true">
        <h3>修订、重算与装包追踪</h3>
        <p class="detail-subtitle">
          {{ businessRef(detailRow) }} · {{ meta.name }} · 同一次修订的纠错端与字幕包端视图
        </p>

        <div v-if="workflowMessage" class="workflow-message" data-testid="workflow-message">{{ workflowMessage }}</div>

        <dl class="detail-grid">
          <div><dt>修订编号</dt><dd data-testid="revision-id">{{ traceRevision?.revision_id ?? detailRow['修订编号'] ?? '—' }}</dd></div>
          <div><dt>版本号</dt><dd>{{ traceRevision?.version_code ?? detailRow['版本号'] ?? detailRow['绑定版本'] ?? '—' }}</dd></div>
          <div><dt>父版本</dt><dd>{{ traceRevision?.parent_version ?? traceSource?.parent_version ?? detailRow['父版本号'] ?? '—' }}</dd></div>
          <div><dt>来源纠错单</dt><dd data-testid="revision-source">{{ traceSource?.correction_id ?? traceRevision?.source_correction_id ?? detailRow['来源纠错单'] ?? '—' }}</dd></div>
          <div><dt>重算编号</dt><dd data-testid="recompute-id">{{ traceRecompute?.recompute_id ?? detailRow['重算编号'] ?? '—' }}</dd></div>
          <div><dt>校验和</dt><dd data-testid="revision-checksum">{{ traceRevision?.checksum ?? detailRow['checksum'] ?? '—' }}</dd></div>
          <div><dt>装包状态</dt><dd data-testid="pack-status">{{ tracePack?.status ?? tracePack?.confirmation_status ?? detailRow.status }}</dd></div>
          <div><dt>最近续办端</dt><dd data-testid="continued-from">{{ tracePack?.last_continued_from ?? traceRevision?.last_continued_from ?? '—' }}</dd></div>
        </dl>

        <div v-if="traceRevision?.continued_from?.length || tracePack?.continued_from?.length" class="continuation-list" data-testid="continuation-list">
          续办来源：{{ Array.from(new Set([...(traceRevision?.continued_from ?? []), ...(tracePack?.continued_from ?? [])])).join('、') }}
        </div>

        <div v-if="traceRecompute?.completion_basis || traceRevision?.completion_basis" class="completion-basis" data-testid="completion-basis">
          完成依据：{{ JSON.stringify(traceRecompute?.completion_basis ?? traceRevision?.completion_basis) }}
        </div>

        <label class="create-field">
          <span>重算正文 / 字幕文件内容</span>
          <pre class="content-preview" data-testid="revision-content">{{ traceContent || '暂无正文' }}</pre>
        </label>

        <div class="page-actions workflow-actions">
          <button v-if="meta.key === 'correction_loop'" class="btn primary" type="button" :disabled="workflowBusy" data-testid="detail-merge" @click="mergeFromCorrection">
            合入并传递正文
          </button>
          <button v-if="meta.key === 'correction_loop'" class="btn" type="button" :disabled="workflowBusy" data-testid="detail-resubmit" @click="resubmitCorrection">
            原单重报 / 续办
          </button>
          <button v-if="meta.key === 'subtitle_pack'" class="btn" type="button" :disabled="workflowBusy" data-testid="detail-continue-pack" @click="continuePack">
            从字幕包页继续生成
          </button>
          <button v-if="meta.key === 'subtitle_pack'" class="btn" type="button" :disabled="workflowBusy" data-testid="detail-confirm-pack" @click="confirmPack">
            核验确认
          </button>
          <button v-if="meta.key === 'subtitle_pack'" class="btn primary" type="button" :disabled="workflowBusy" data-testid="detail-claim-pack" @click="claimPack">
            领取
          </button>
          <button v-if="meta.key === 'subtitle_pack'" class="btn primary" type="button" :disabled="workflowBusy" data-testid="detail-confirm-claim" @click="claimAndConfirmPack">
            确认并领取
          </button>
          <button class="btn" type="button" data-testid="detail-download" @click="downloadTraceFile">下载字幕/版本</button>
          <button class="btn ghost" type="button" @click="closeDetail">关闭</button>
        </div>
      </section>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import {
  createEntry,
  downloadEntries,
  getWorkflowTrace,
  listEntries,
  moduleMeta,
  postWorkflow,
  runAction as applyAction,
  workflowDownload,
} from '@/api/backend-service'
import type { EntryRow } from '@/data/types'

const props = defineProps<{ moduleKey: string }>()
const meta = moduleMeta(props.moduleKey)
const rows = ref<EntryRow[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = meta.fields.slice(0, 3)
const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})
const detailRow = ref<EntryRow | null>(null)
const trace = ref<any>(null)
const workflowBusy = ref(false)
const workflowMessage = ref('')
const successMessage = ref('')

const TRACE_MODULES = new Set(['correction_loop', 'transcript_version', 'subtitle_pack'])
const supportsTrace = computed(() => TRACE_MODULES.has(props.moduleKey))
const detailRef = computed(() => businessRef(detailRow.value))
const traceRevision = computed<any>(() => trace.value?.revision ?? detailRow.value?.revision ?? detailRow.value?.workflow?.revision ?? null)
const tracePack = computed<any>(() => trace.value?.pack ?? detailRow.value?.pack ?? detailRow.value?.workflow?.pack ?? null)
const traceContent = computed(() => {
  const candidate = trace.value?.content
    ?? traceRevision.value?.content
    ?? detailRow.value?.['正文']
    ?? detailRow.value?.content
    ?? ''
  return String(candidate)
})
const traceSource = computed<any>(() => trace.value?.source ?? traceRevision.value?.来源 ?? traceRevision.value?.source ?? null)
const traceRecompute = computed<any>(() => trace.value?.recompute ?? traceRevision.value?.recompute ?? detailRow.value?.recompute ?? null)
const stats = computed(() => [
  { label: meta.metrics[0], value: total.value },
  { label: meta.metrics[1], value: rows.value.filter((row) => row.status === meta.statuses[1]).length },
  {
    label: meta.metrics[2],
    value: rows.value.filter((row) => row.status === meta.statuses[meta.statuses.length - 1]).length,
  },
])
const statusSummary = computed(() =>
  meta.statuses.map((status) => ({
    status,
    count: rows.value.filter((row) => String(row.status) === status).length,
  })),
)

function resetFilters() {
  filters.value = {}
  void reload()
}

async function exportRows() {
  errorMessage.value = ''
  try {
    await downloadEntries(meta.key, filters.value)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出失败'
  }
}

function openCreate() {
  createValues.value = Object.fromEntries(meta.fields.map((field) => [field, '']))
  errorMessage.value = ''
  showCreate.value = true
}

async function submitCreate() {
  creating.value = true
  errorMessage.value = ''
  try {
    const result = await createEntry(meta.key, createValues.value)
    if (!result.ok) throw new Error(result.message)
    showCreate.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '登记失败'
  } finally {
    creating.value = false
  }
}

async function runAction(action: string, row: EntryRow) {
  errorMessage.value = ''
  successMessage.value = ''
  workflowMessage.value = ''
  try {
    const result = await applyAction(meta.key, Number(row.id), action)
    if (!result.ok) throw new Error(result.message)
    successMessage.value = result.message
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '操作失败'
  }
}

function businessRef(row: EntryRow | null): string {
  if (!row) return ''
  const fieldMap: Record<string, string> = {
    correction_loop: '纠错编号',
    transcript_version: '版本编号',
    subtitle_pack: '字幕包编号',
  }
  const field = fieldMap[meta.key]
  return String(row[field] ?? row.id)
}

function tracePath(row: EntryRow | null): string {
  const ref = encodeURIComponent(businessRef(row))
  if (meta.key === 'correction_loop') return `/api/correction_loop/${ref}/revision`
  if (meta.key === 'subtitle_pack') return `/api/subtitle_pack/${ref}/revision`
  return `/api/transcript_version/${ref}/revision`
}

async function openDetail(row: EntryRow) {
  detailRow.value = row
  trace.value = row.workflow ?? row.revision ? { revision: row.revision, pack: row.pack, ...row.workflow } : null
  errorMessage.value = ''
  workflowMessage.value = ''
  try {
    const result = await getWorkflowTrace(tracePath(row))
    trace.value = result
    if (!result.ok) workflowMessage.value = result.message
  } catch (error) {
    workflowMessage.value = error instanceof Error ? error.message : '修订追踪查询失败'
  }
}

function closeDetail() {
  detailRow.value = null
  trace.value = null
  workflowMessage.value = ''
}

async function runTraceWorkflow(path: string, body: Record<string, unknown> = {}) {
  workflowBusy.value = true
  workflowMessage.value = ''
  errorMessage.value = ''
  try {
    const result = await postWorkflow(path, body)
    trace.value = result
    workflowMessage.value = result.message ?? ''
    if (!result.ok) errorMessage.value = result.message ?? '工作流未完成'
    await reload()
    if (detailRow.value) {
      const refreshed = rows.value.find((row) => businessRef(row) === detailRef.value)
      if (refreshed) detailRow.value = refreshed
    }
    return result
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '工作流请求失败'
    return null
  } finally {
    workflowBusy.value = false
  }
}

function mergeFromCorrection() {
  const ref = detailRef.value
  return runTraceWorkflow(`/api/correction_loop/${encodeURIComponent(ref)}/merge`, {})
}

function resubmitCorrection() {
  const ref = detailRef.value
  return runTraceWorkflow(`/api/correction_loop/${encodeURIComponent(ref)}/resubmit`, {})
}

function continuePack() {
  const ref = tracePack.value?.pack_id ?? detailRef.value
  return runTraceWorkflow(`/api/subtitle_pack/${encodeURIComponent(String(ref))}/continue`, {})
}

function confirmPack() {
  const ref = tracePack.value?.pack_id ?? detailRef.value
  return runTraceWorkflow(`/api/subtitle_pack/${encodeURIComponent(String(ref))}/confirm`, {})
}

function claimPack() {
  const ref = tracePack.value?.pack_id ?? detailRef.value
  return runTraceWorkflow(`/api/subtitle_pack/${encodeURIComponent(String(ref))}/claim`, {})
}

function claimAndConfirmPack() {
  const ref = tracePack.value?.pack_id ?? detailRef.value
  return runTraceWorkflow(`/api/subtitle_pack/${encodeURIComponent(String(ref))}/claim`, { confirm: true })
}

function downloadTraceFile() {
  const url = tracePack.value?.pack_id
    ? `/api/subtitle_pack/${encodeURIComponent(tracePack.value.pack_id)}/download`
    : `/api/transcript_version/${encodeURIComponent(detailRef.value)}/download`
  workflowDownload(url)
}

function downloadRow(row: EntryRow) {
  const ref = encodeURIComponent(businessRef(row))
  let url = `/api/transcript_version/${ref}/download`
  if (meta.key === 'subtitle_pack') {
    url = `/api/subtitle_pack/${ref}/download`
  } else if (meta.key === 'correction_loop') {
    const revisionRef = String(row['修订编号'] ?? row['_revision_id'] ?? row.revision_id ?? businessRef(row))
    url = `/api/revisions/${encodeURIComponent(revisionRef)}/download`
  }
  workflowDownload(url)
}

async function reload() {
  errorMessage.value = ''
  try {
    const payload = await listEntries(meta.key, filters.value)
    rows.value = payload.items
    total.value = payload.total
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '业务列表读取失败'
  }
}

onMounted(reload)
</script>
