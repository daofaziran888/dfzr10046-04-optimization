<template>
  <section class="page release-page" data-testid="release-control">
    <header class="page-head">
      <div>
        <h2>放行交接</h2>
        <p class="page-desc">
          质量闸放行确认与装载窗口交接：确认接口返回换班语境下的实际结论，
          只有实际放行才写入装载待办，装载员凭有效回执才能推进。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn" data-testid="release-refresh" type="button" @click="reload">
          刷新交接看板
        </button>
        <button class="btn ghost" data-testid="release-reset" type="button" @click="resetDemo">
          重置演示数据
        </button>
      </div>
    </header>

    <p v-if="flash" class="release-flash" :class="{ bad: !flashOk }" data-testid="release-flash">
      {{ flash }}
    </p>

    <div class="stat-row">
      <article class="stat-card" data-testid="current-version">
        <span class="stat-label">生效阈值版本</span>
        <strong class="stat-value">{{ snapshot?.current_version.version_label ?? '—' }}</strong>
        <span class="stat-note">
          阈值 {{ snapshot?.current_version.threshold ?? '—' }} ·
          上限 {{ snapshot?.current_version.cap ?? '—' }} ·
          {{ snapshot?.current_version.handover_policy_label ?? '' }}
        </span>
      </article>
      <article class="stat-card" data-testid="seat-capacity">
        <span class="stat-label">当班名额</span>
        <strong class="stat-value">
          {{ snapshot?.capacity.used ?? 0 }} / {{ snapshot?.capacity.limit ?? '—' }}
        </strong>
        <span class="stat-note">已放行与受理占位计入名额</span>
      </article>
      <article class="stat-card" data-testid="pending-count">
        <span class="stat-label">待确认申请</span>
        <strong class="stat-value">{{ snapshot?.pending_count ?? 0 }}</strong>
        <span class="stat-note">确认框会按现行阈值重新判定</span>
      </article>
      <article class="stat-card" data-testid="window-state">
        <span class="stat-label">装载窗口</span>
        <strong class="stat-value">{{ activeWindow?.state_label ?? '开启' }}</strong>
        <span class="stat-note">
          {{ activeWindow?.changed_at ? `变更于 ${formatTime(activeWindow.changed_at)}` : '暂无窗口变更' }}
        </span>
      </article>
    </div>

    <section class="release-grid">
      <article class="panel">
        <h3>换班阈值版本</h3>
        <form class="release-form" @submit.prevent="submitVersion">
          <label>
            <span>阈值</span>
            <input
              v-model.number="versionForm.threshold"
              data-testid="threshold-input"
              type="number"
              min="0"
              max="1"
              step="0.01"
            />
          </label>
          <label>
            <span>当班上限</span>
            <input
              v-model.number="versionForm.cap"
              data-testid="cap-input"
              type="number"
              min="0"
              step="1"
            />
          </label>
          <label>
            <span>换班策略</span>
            <select v-model="versionForm.handover_policy" data-testid="policy-select">
              <option
                v-for="policy in policyOptions"
                :key="policy.value"
                :value="policy.value"
              >
                {{ policy.label }}
              </option>
            </select>
          </label>
          <label>
            <span>生效时刻</span>
            <input v-model="versionForm.effective_at" data-testid="effective-at" type="datetime-local" />
          </label>
          <label>
            <span>调整人</span>
            <input v-model="versionForm.updated_by" data-testid="updated-by" />
          </label>
          <button class="btn primary" data-testid="submit-version" type="submit">
            调整阈值并重新判定
          </button>
        </form>

        <div class="release-actions">
          <button class="btn" data-testid="close-window" type="button" @click="changeWindow('closed')">
            关闭装载窗口
          </button>
          <button class="btn" data-testid="open-window" type="button" @click="changeWindow('open')">
            重新开启窗口
          </button>
        </div>

        <table class="data-table compact" data-testid="version-table">
          <thead>
            <tr><th>版本</th><th>阈值</th><th>上限</th><th>策略</th><th>生效时刻</th><th>调整人</th></tr>
          </thead>
          <tbody>
            <tr
              v-for="version in snapshot?.versions ?? []"
              :key="version.version"
              :data-testid="`version-${version.version}`"
            >
              <td>{{ version.version_label }}<span v-if="version.active"> ·生效</span></td>
              <td>{{ version.threshold }}</td>
              <td>{{ version.cap }}</td>
              <td>{{ policyLabel(version.handover_policy) }}</td>
              <td>{{ formatTime(version.effective_at) }}</td>
              <td>{{ version.updated_by }}</td>
            </tr>
          </tbody>
        </table>
      </article>

      <article class="panel">
        <h3>质量闸放行申请</h3>
        <p class="panel-note">
          受理时冻结阈值版本与名额语义；窗口关闭时新申请直接被拒，不进入确认框。
        </p>
        <form class="release-form" @submit.prevent="submitApplication">
          <label>
            <span>闸口编号</span>
            <input v-model="appForm.gate_id" data-testid="app-gate" required />
          </label>
          <label>
            <span>校验项</span>
            <input v-model="appForm.check_item" data-testid="app-check" />
          </label>
          <label>
            <span>实测读数</span>
            <input
              v-model.number="appForm.measured"
              data-testid="app-measured"
              type="number"
              min="0"
              max="1"
              step="0.01"
              required
            />
          </label>
          <label>
            <span>装载窗口编号</span>
            <input v-model="appForm.window_id" data-testid="app-window" />
          </label>
          <label>
            <span>窗口日期</span>
            <input v-model="appForm.window_date" data-testid="app-window-date" type="date" />
          </label>
          <label>
            <span>请求号</span>
            <input
              v-model="appForm.client_request_id"
              data-testid="app-request-id"
              placeholder="同一请求号重复提交只会受理一次"
            />
          </label>
          <button class="btn primary" data-testid="submit-application" type="submit">
            提交放行申请
          </button>
        </form>
        <p v-if="lastSubmit" class="panel-note" data-testid="submit-result">{{ lastSubmit }}</p>

        <h3>质量闸放行确认</h3>
        <p class="panel-note">
          确认框按现行阈值版本重新判定：只有实际放行才签发回执并写入装载待办。
        </p>
        <div
          v-if="pendingJudgements.length"
          class="confirm-dialog"
          data-testid="release-confirm-dialog"
        >
          <div
            v-for="item in pendingJudgements"
            :key="item.application_id"
            class="confirm-row"
            :data-testid="`confirm-${item.application_id}`"
            :data-outcome="item.outcome"
          >
            <div class="confirm-head">
              <strong>{{ item.application_id }}</strong>
              <span class="tag" :class="item.outcome">{{ item.outcome_label }}</span>
            </div>
            <p class="confirm-line">
              实测 {{ item.check_item }} {{ item.measured.toFixed(3) }} ·
              按 v{{ item.threshold_version }} 阈值 {{ item.threshold }} ·
              名额 {{ item.capacity.after_confirm }}/{{ item.capacity.limit }}
            </p>
            <p class="confirm-reason" :data-testid="`reason-${item.application_id}`">
              {{ item.reason }}
            </p>
            <p class="confirm-handover">
              {{ item.handover.reason }}
            </p>
            <div class="confirm-actions">
              <button
                class="btn primary"
                :data-testid="`confirm-${item.application_id}-button`"
                type="button"
                @click="confirmApplication(item.application_id)"
              >
                确认放行
              </button>
              <button
                class="btn"
                :data-testid="`retry-${item.application_id}`"
                type="button"
                @click="retryConfirm(item)"
              >
                模拟网络重试
              </button>
              <button
                class="btn"
                :data-testid="`recheck-${item.application_id}`"
                type="button"
                @click="reload"
              >
                重新判定
              </button>
            </div>
          </div>
        </div>
        <p v-else class="empty-state" data-testid="confirm-dialog-empty">
          没有待确认的放行申请
        </p>

        <h4 class="panel-subtitle">已确认结论</h4>
        <ul class="release-timeline" data-testid="application-log">
          <li
            v-for="item in decidedApplications"
            :key="item.app_id"
            :data-testid="`app-${item.app_id}`"
            :data-state="item.state"
          >
            <strong>{{ item.app_id }}</strong>
            <span class="tag" :class="item.state === '已放行' ? 'released' : 'rejected'">
              {{ item.state }}
            </span>
            <span class="timeline-reason">{{ item.judgement?.reason ?? '' }}</span>
          </li>
          <li v-if="!decidedApplications.length" class="empty-state">暂无已确认申请</li>
        </ul>
      </article>
    </section>

    <section class="panel" data-testid="handover-panel">
      <h3>切班交接查询</h3>
      <p class="panel-note" data-testid="handover-message">{{ handover?.message ?? '加载中…' }}</p>
      <table class="data-table compact" v-if="handover?.handover_decisions.length" data-testid="handover-table">
        <thead>
          <tr>
            <th>申请</th><th>窗口</th><th>受理版本</th><th>策略</th>
            <th>生效时结论</th><th>可否交班</th><th>交接说明</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="decision in handover.handover_decisions"
            :key="decision.application_id"
            :data-testid="`handover-${decision.application_id}`"
            :data-can-handover="String(decision.can_handover)"
          >
            <td>{{ decision.application_id }}</td>
            <td>{{ decision.window_id }}</td>
            <td>{{ decision.accepted_version }}</td>
            <td>{{ policyLabel(decision.policy) }}</td>
            <td>{{ decision.outcome_at_change }}</td>
            <td>{{ decision.can_handover ? '可交班' : '不可交班' }}</td>
            <td class="timeline-reason">{{ decision.handover_reason }}</td>
          </tr>
        </tbody>
      </table>
      <p class="panel-note">
        判定优先级：
        <span v-for="(item, index) in snapshot?.evaluation_priority ?? []" :key="item">
          {{ index + 1 }}. {{ priorityLabel(item) }}
          <span v-if="index < (snapshot?.evaluation_priority.length ?? 0) - 1"> &gt; </span>
        </span>
      </p>
    </section>

    <section class="panel" data-testid="load-todo-panel">
      <h3>装载待办与回执</h3>
      <p class="panel-note">
        只有实际放行的申请才在这里出现；被拒或尚待结论的申请没有回执，装载确认会被显式挡住。
      </p>
      <table class="data-table" data-testid="load-todo-table">
        <thead>
          <tr>
            <th>窗口编号</th><th>放行回执</th><th>判定编号</th><th>阈值版本</th>
            <th>状态</th><th>窗口起点</th><th>预期行数</th><th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="row in todos"
            :key="String(row.id)"
            :data-testid="`todo-${row.id}`"
            :data-receipt="String(row['放行回执'] ?? '')"
          >
            <td>{{ row['窗口编号'] }}</td>
            <td>
              <span :data-testid="`todo-${row.id}-receipt`">{{ row['放行回执'] ?? '—' }}</span>
              <span
                v-if="receiptState(String(row['放行回执'] ?? ''))"
                class="tag"
                :class="receiptState(String(row['放行回执'] ?? ''))"
              >{{ receiptState(String(row['放行回执'] ?? '')) }}</span>
            </td>
            <td>{{ row['判定编号'] }}</td>
            <td>{{ row['阈值版本'] }}</td>
            <td>{{ row['status'] }}</td>
            <td>{{ row['窗口起点'] }}</td>
            <td>{{ row['预期行数'] }}</td>
            <td class="row-actions">
              <button
                class="link"
                :data-testid="`todo-${row.id}-load`"
                type="button"
                @click="confirmLoad(row)"
              >
                凭回执确认装载
              </button>
            </td>
          </tr>
          <tr v-if="!todos.length">
            <td colspan="8" class="empty-state">暂无装载待办，先完成质量闸放行确认</td>
          </tr>
        </tbody>
      </table>
    </section>

    <section class="panel" v-if="unresolved">
      <h3>回包核对</h3>
      <p class="panel-note" data-testid="unresolved-note">
        上一笔确认回包不明，重新打开质量闸核对到的实际结论如下：
      </p>
      <pre class="release-pre" data-testid="unresolved-json">{{ prettyUnresolved }}</pre>
    </section>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'

import {
  adjustThresholdVersion,
  confirmLoadWindow,
  confirmReleaseApplication,
  loadHandoverProjection,
  loadReleaseControl,
  readApplicationConclusion,
  resetReleaseDemo,
  setLoadWindow,
  submitReleaseApplication,
} from '@/api/release-service'
import type {
  ReleaseControl,
  ReleaseDetail,
  ReleaseJudgement,
} from '@/api/release-service'

type HandoverProjection = Awaited<ReturnType<typeof loadHandoverProjection>>
type TodoRow = Record<string, unknown>

const snapshot = ref<ReleaseControl | null>(null)
const handover = ref<HandoverProjection | null>(null)
const unresolved = ref<ReleaseDetail | null>(null)
const flash = ref('')
const flashOk = ref(true)
const lastSubmit = ref('')
const versionForm = reactive({
  threshold: 0.85,
  cap: 2,
  handover_policy: 'recompute',
  effective_at: '',
  updated_by: '值班员',
})
const appForm = reactive({
  gate_id: 'QG-NEW',
  check_item: '外键命中率',
  measured: 0.92,
  window_id: '',
  window_date: '2026-11-26',
  client_request_id: '',
})

const factTable = computed(() => {
  const tables = Object.keys(snapshot.value?.windows ?? {})
  return tables[0] ?? '销售事实表'
})
const activeWindow = computed(() => snapshot.value?.windows[factTable.value] ?? null)
const todos = computed<TodoRow[]>(() => (snapshot.value?.todos ?? []) as TodoRow[])
const pendingJudgements = computed<ReleaseJudgement[]>(() => snapshot.value?.previews ?? [])
const decidedApplications = computed(() =>
  (snapshot.value?.applications ?? []).filter((item) => item.state !== '待确认'),
)
const policyOptions = computed(
  () => snapshot.value?.shift_change.handover_policies ?? [
    { value: 'recompute', label: '放行重算' },
    { value: 'reserve', label: '受理占位' },
  ],
)
const prettyUnresolved = computed(() =>
  unresolved.value ? JSON.stringify(unresolved.value.actual_conclusion, null, 2) : '',
)

function policyLabel(value: string): string {
  return policyOptions.value.find((item) => item.value === value)?.label ?? value
}

const PRIORITY_LABELS: Record<string, string> = {
  window_closed: '窗口关闭',
  handover_policy: '换班策略',
  threshold: '阈值',
  capacity: '名额',
}

function priorityLabel(value: string): string {
  return PRIORITY_LABELS[value] ?? value
}

function formatTime(value: string | null | undefined): string {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleString('zh-CN', { hour12: false })
}

function receiptState(receiptId: string): string {
  if (!receiptId) return ''
  const receipt = snapshot.value?.receipts.find((item) => item.receipt_id === receiptId)
  if (!receipt) return ''
  return receipt.state === 'valid' ? '有效回执' : '已核销'
}

function requestId(prefix: string): string {
  return `${prefix}-${Date.now()}-${Math.random().toString(16).slice(2, 8)}`
}

function showFlash(message: string, ok: boolean) {
  flash.value = message
  flashOk.value = ok
}

async function reload() {
  try {
    const [control, projection] = await Promise.all([
      loadReleaseControl(),
      loadHandoverProjection(),
    ])
    snapshot.value = control
    handover.value = projection
    if (!versionForm.effective_at) {
      versionForm.threshold = control.current_version.threshold
      versionForm.cap = control.current_version.cap
      versionForm.handover_policy = control.current_version.handover_policy
    }
  } catch (error) {
    showFlash(error instanceof Error ? error.message : '交接看板读取失败', false)
  }
}

async function resetDemo() {
  try {
    const result = await resetReleaseDemo()
    showFlash(result.message, result.ok)
    unresolved.value = null
    await reload()
  } catch (error) {
    showFlash(error instanceof Error ? error.message : '重置失败', false)
  }
}

async function submitVersion() {
  if (!snapshot.value) return
  try {
    const result = await adjustThresholdVersion({
      threshold: versionForm.threshold,
      cap: versionForm.cap,
      handover_policy: versionForm.handover_policy,
      effective_at: versionForm.effective_at
        ? new Date(versionForm.effective_at).toISOString()
        : undefined,
      updated_by: versionForm.updated_by || '值班员',
      reason: '值班员在交接看板调整',
    })
    showFlash(result.message, result.ok)
    await reload()
  } catch (error) {
    showFlash(error instanceof Error ? error.message : '阈值调整失败', false)
  }
}

async function changeWindow(state: 'open' | 'closed') {
  if (!snapshot.value) return
  try {
    const result = await setLoadWindow({
      fact_table: factTable.value,
      state,
      by: '值班员',
      reason: state === 'closed' ? '换班关闭装载窗口' : '换班重新开启装载窗口',
    })
    showFlash(result.message, result.ok)
    await reload()
  } catch (error) {
    showFlash(error instanceof Error ? error.message : '窗口状态变更失败', false)
  }
}

async function confirmApplication(appId: string) {
  try {
    const result = await confirmReleaseApplication(appId, requestId('confirm'))
    showFlash(result.message, result.ok)
    unresolved.value = await readApplicationConclusion(appId)
    await reload()
  } catch (error) {
    // 回包不明（网络错误/5xx）时重新打开质量闸核对实际结论
    showFlash(error instanceof Error ? error.message : '确认回包不明', false)
    try {
      unresolved.value = await readApplicationConclusion(appId)
    } catch {
      unresolved.value = null
    }
    await reload()
  }
}

async function confirmLoad(row: TodoRow) {
  const receiptId = String(row['放行回执'] ?? '') || null
  try {
    const result = await confirmLoadWindow(Number(row.id), receiptId)
    showFlash(result.message, result.ok)
    await reload()
  } catch (error) {
    showFlash(error instanceof Error ? error.message : '装载确认失败', false)
  }
}

async function submitApplication() {
  lastSubmit.value = ''
  const clientRequestId = appForm.client_request_id.trim() || requestId('app')
  try {
    const result = await submitReleaseApplication({
      gate_id: appForm.gate_id,
      fact_table: factTable.value,
      check_item: appForm.check_item,
      measured: appForm.measured,
      window_id: appForm.window_id || undefined,
      window_start: appForm.window_date || undefined,
      window_end: appForm.window_date || undefined,
      client_request_id: clientRequestId,
    })
    showFlash(result.message, result.ok)
    lastSubmit.value = result.message
    await reload()
  } catch (error) {
    showFlash(error instanceof Error ? error.message : '放行申请提交失败', false)
  }
}

async function retryConfirm(item: ReleaseJudgement) {
  // 模拟网络延迟导致的重复确认：沿用同一请求号，服务端只保留一份回执。
  try {
    const result = await confirmReleaseApplication(item.application_id, `retry-${item.application_id}`)
    showFlash(result.message, result.ok)
    await reload()
  } catch (error) {
    showFlash(error instanceof Error ? error.message : '重试确认失败', false)
  }
}

onMounted(reload)
</script>
