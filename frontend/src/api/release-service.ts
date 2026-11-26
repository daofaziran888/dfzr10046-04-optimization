/** 放行交接接口：质量闸放行确认、阈值版本、窗口状态、回执与装载待办。 */
import { request } from './client'

export type ReleaseVersion = {
  version: number
  version_label: string
  threshold: number
  cap: number
  handover_policy: string
  handover_policy_label: string
  shift_id: string
  effective_at: string
  created_at: string
  updated_by: string
  reason: string
  active: boolean
  pending: boolean
}

export type ReleaseJudgement = {
  judgement_id: string
  preview: boolean
  application_id: string
  gate_id: string
  fact_table: string
  check_item: string
  measured: number
  window_id: string
  evaluated_at: string
  outcome: 'released' | 'rejected' | 'pending'
  outcome_label: string
  reason_code: string
  reason: string
  threshold_version: number
  threshold: number
  cap: number
  handover_policy: string
  handover_policy_label: string
  shift_id: string
  window_state: 'open' | 'closed'
  capacity: {
    used: number
    limit: number
    after_confirm: number
    at_cap: boolean
    remaining: number
  }
  handover: {
    in_shift_change: boolean
    accepted_version: number
    accepted_version_label: string
    effective_version: number
    effective_version_label: string
    current_version: number
    current_version_effective_at: string
    policy: string
    seat_reserved: boolean
    can_handover: boolean
    reason: string
  }
  checks: { priority: number; check: string; status: string; detail: string }[]
  priority: string[]
}

export type ReleaseApplication = {
  app_id: string
  gate_id: string
  fact_table: string
  check_item: string
  measured: number
  window_id: string
  window_start: string
  window_end: string
  expected_rows: number
  submitted_at: string
  client_request_id: string
  received_version: number
  received_version_label: string
  received_threshold: number
  received_cap: number
  received_policy: string
  shift_id: string
  reviewer: string
  state: '待确认' | '已放行' | '已拒绝'
  seat_reserved: boolean
  seat_version: number | null
  receipt_id: string | null
  judgement: ReleaseJudgement | null
}

export type ReleaseReceipt = {
  receipt_id: string
  app_id: string
  judgement_id: string
  threshold_version: number
  threshold: number
  issued_at: string
  state: 'valid' | 'consumed' | 'revoked'
  valid: boolean
  consumed_window_id: number | null
  consumed_at: string | null
  duplicate_suppressed: number
}

export type WindowSnapshot = {
  fact_table: string
  state: 'open' | 'closed'
  state_label: string
  reason: string
  by: string
  changed_at: string | null
  events: { state: string; state_label: string; reason: string; by: string; changed_at: string }[]
  scheduled_events?: { state: string; state_label: string; reason: string; by: string; changed_at: string }[]
  reopenable: boolean
}

export type ReleaseControl = {
  current_version: ReleaseVersion
  versions: ReleaseVersion[]
  next_version: ReleaseVersion | null
  next_change_at: string | null
  shift_change: {
    active_version: number
    effective_at: string
    handover_policy: string
    handover_policy_label: string
    handover_policies: { value: string; label: string }[]
    priority: string[]
  }
  windows: Record<string, WindowSnapshot>
  applications: ReleaseApplication[]
  previews: ReleaseJudgement[]
  pending_count: number
  capacity: { version: number; used: number; limit: number; remaining: number }
  receipts: ReleaseReceipt[]
  todos: Record<string, unknown>[]
  evaluation_priority: string[]
}

export type ReleaseDetail = {
  application: ReleaseApplication
  judgement: ReleaseJudgement | null
  receipt: ReleaseReceipt | null
  todo: Record<string, unknown> | null
  actual_conclusion?: {
    state: string
    outcome: string
    reason_code: string
    reason: string
    receipt_id: string | null
    receipt_state: string | null
  }
}

export type ReleaseMutation = {
  ok: boolean
  message: string
  entry: {
    judgement?: ReleaseJudgement | null
    receipt?: ReleaseReceipt | null
    todo?: Record<string, unknown> | null
    application?: ReleaseApplication | null
    previews?: ReleaseJudgement[]
    window?: WindowSnapshot
    [key: string]: unknown
  } | null
}

async function readJson<T>(response: Response): Promise<T> {
  if (!response.ok) {
    const detail = await response.text()
    throw new Error(`接口返回 ${response.status}：${detail.slice(0, 120)}`)
  }
  return (await response.json()) as T
}

function jsonBody(payload: unknown): RequestInit {
  return { method: 'POST', body: JSON.stringify(payload ?? {}) }
}

export async function loadReleaseControl(): Promise<ReleaseControl> {
  return readJson<ReleaseControl>(await request('/api/release/control'))
}

export async function resetReleaseDemo(): Promise<ReleaseMutation> {
  return readJson<ReleaseMutation>(await request('/api/release/reset', jsonBody({})))
}

export async function adjustThresholdVersion(payload: {
  threshold?: number
  cap?: number
  handover_policy?: string
  effective_at?: string
  updated_by: string
  reason?: string
}): Promise<ReleaseMutation> {
  return readJson<ReleaseMutation>(await request('/api/release/versions', jsonBody(payload)))
}

export async function setLoadWindow(payload: {
  fact_table: string
  state: 'open' | 'closed'
  by?: string
  reason?: string
  effective_at?: string
}): Promise<ReleaseMutation> {
  return readJson<ReleaseMutation>(await request('/api/release/windows', jsonBody(payload)))
}

export async function submitReleaseApplication(payload: {
  gate_id: string
  fact_table: string
  check_item?: string
  measured: number
  window_id?: string
  window_start?: string
  window_end?: string
  expected_rows?: number
  handover_policy?: string
  reviewer?: string
  client_request_id?: string
}): Promise<ReleaseMutation> {
  return readJson<ReleaseMutation>(
    await request('/api/release/applications', jsonBody(payload)),
  )
}

export async function confirmReleaseApplication(
  appId: string,
  clientRequestId: string,
): Promise<ReleaseMutation> {
  return readJson<ReleaseMutation>(
    await request(`/api/release/applications/${encodeURIComponent(appId)}/confirm`,
      jsonBody({ client_request_id: clientRequestId })),
  )
}

export async function readApplicationConclusion(appId: string): Promise<ReleaseDetail> {
  return readJson<ReleaseDetail>(
    await request(`/api/release/applications/${encodeURIComponent(appId)}`),
  )
}

export async function confirmLoadWindow(
  windowId: number | string,
  receiptId: string | null,
): Promise<ReleaseMutation> {
  return readJson<ReleaseMutation>(
    await request(`/api/release/windows/${encodeURIComponent(String(windowId))}/load`,
      jsonBody({ receipt_id: receiptId })),
  )
}

export async function loadHandoverProjection(): Promise<{
  next_change_at: string | null
  message: string
  handover_decisions: {
    application_id: string
    gate_id: string
    window_id: string
    accepted_version: string
    policy: string
    policy_label: string
    seat_reserved: boolean
    outcome_at_change: string
    reason_code: string
    can_handover: boolean
    handover_reason: string
  }[]
}> {
  return readJson(await request('/api/release/handover'))
}
