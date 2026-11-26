/**
 * 失败恢复重排 API：恢复候选单、重排申请、迟到回执确认、计划恢复关联。
 * 与通用资源接口分离，承载失败隔离区 -> 采集调度计划的真实业务链路。
 */
import { request } from './client'

export type 候选状态 = '可重排' | '已处理' | '候选失效' | '本次入选' | '待核验'

export type RecoveryCandidate = {
  id: number
  样本编号: string
  样本指纹: string
  来源站点: string
  站点标识: string
  失败阶段: string
  失败原因: string
  隔离时间: string
  重试次数: string
  status: string
  候选状态: 候选状态
  不可选原因: string
  回执状态: string
  回执待确认: boolean
  处理方式: string
  重排任务: string
}

export type CandidatePage = {
  ok: boolean
  message: string
  filters: { 来源站点: string; 站点标识: string; 失败阶段: string }
  page: number
  size: number
  total: number
  eligible_total: number
  items: RecoveryCandidate[]
}

export type RescheduleResult = {
  quarantine_id: number
  样本编号: string
  来源站点?: string
  站点标识?: string
  失败阶段?: string
  status?: string
  state: 候选状态
  reason: string
  dispatched: boolean
  task_id?: string
  回执?: string
  request_id?: number
}

export type RescheduleTask = {
  id: number
  task_id: string
  request_id: number | null
  计划id: number | null
  计划名称: string
  样本编号: string
  样本指纹: string
  来源站点: string
  站点标识: string
  失败阶段: string
  任务类型: string
  回执: string
  status: string
  创建时间: string
}

export type RescheduleResponse = {
  ok: boolean
  message: string
  request_id: number
  request_no: string
  results: RescheduleResult[]
  tasks: RescheduleTask[]
}

export type PlanScope = {
  ok: boolean
  message: string
  plan: { id: number; 计划名称: string; status: string }
  linked_tasks: {
    task_id: string
    样本编号: string
    来源站点: string
    站点标识: string
    失败阶段: string
    任务类型: string
    回执: string
    task_status: string
    隔离项状态: string
    隔离项处理方式: string
  }[]
  linked_total: number
  recoverable_scope: RecoveryCandidate[]
  recoverable_total: number
}

async function asJson<T>(response: Response): Promise<T> {
  const body = (await response.json()) as T & { message?: string }
  if (!response.ok) throw new Error(body.message ?? `接口返回 ${response.status}`)
  return body
}

export async function fetchCandidates(params: {
  来源站点?: string
  站点标识?: string
  失败阶段?: string
  page: number
  size: number
}): Promise<CandidatePage> {
  const query = new URLSearchParams()
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== null && String(value) !== '') query.set(key, String(value))
  })
  return asJson<CandidatePage>(await request(`/api/recovery/candidates?${query.toString()}`))
}

export async function submitReschedule(payload: {
  candidate_ids: number[]
  来源站点?: string
  失败阶段?: string
  plan_id?: number | null
}): Promise<RescheduleResponse> {
  return asJson<RescheduleResponse>(
    await request('/api/recovery/reschedule', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  )
}

export async function ingestReceipt(样本编号: string): Promise<{ ok: boolean; message: string }> {
  return asJson(
    await request('/api/recovery/receipts', {
      method: 'POST',
      body: JSON.stringify({ 样本编号 }),
    }),
  )
}

export async function confirmReceipt(
  样本编号: string,
  usable: boolean,
): Promise<{ ok: boolean; message: string; action?: string; dispatched?: boolean; task_id?: string }> {
  return asJson(
    await request('/api/recovery/receipts/confirm', {
      method: 'POST',
      body: JSON.stringify({ 样本编号, usable }),
    }),
  )
}

export async function fetchPlanScope(planId: number): Promise<PlanScope> {
  return asJson<PlanScope>(await request(`/api/recovery/plans/${planId}/scope`))
}

export async function changeStage(
  quarantine_id: number,
  失败阶段: string,
): Promise<{ ok: boolean; message: string }> {
  return asJson(
    await request('/api/recovery/stage', {
      method: 'POST',
      body: JSON.stringify({ quarantine_id, 失败阶段 }),
    }),
  )
}
