import { apiRequest } from './client'
import type { DailyMetric } from './orgs'

/** 用量来源：对话 / API 调用 / 知识库处理 / 知识库检索 */
export type UsageSource = 'chat' | 'api' | 'kb_ingest' | 'kb_query'

export const USAGE_SOURCE_OPTIONS: { value: UsageSource; label: string }[] = [
  { value: 'chat', label: '对话' },
  { value: 'api', label: 'API 调用' },
  { value: 'kb_ingest', label: '知识库处理' },
  { value: 'kb_query', label: '知识库检索' },
]

export const USAGE_SOURCE_LABEL: Record<UsageSource, string> = {
  chat: '对话',
  api: 'API 调用',
  kb_ingest: '知识库处理',
  kb_query: '知识库检索',
}

/** 资金来源：纯赠金 / 纯现金 / 混合（两池都 > 0） */
export type FundSource = 'gift' | 'cash' | 'mixed'

export const FUND_SOURCE_OPTIONS: { value: FundSource; label: string }[] = [
  { value: 'gift', label: '赠金' },
  { value: 'cash', label: '现金' },
  { value: 'mixed', label: '混合' },
]

export type UsageLog = {
  id: string
  request_id: string
  user_id: string | null
  model_id: string | null
  input_tokens: number
  output_tokens: number
  cached_tokens: number
  billing_rule_version: number
  cost_amount: number
  charge_amount: number
  gift_charge_amount?: number
  cash_charge_amount?: number
  is_estimated: boolean
  source: UsageSource | string | null
  created_at: string
}

export type UserDailyReport = {
  timezone: string
  from_date: string
  to_date: string
  items: DailyMetric[]
}

export function formatUsageSource(source: string | null | undefined): string {
  if (!source) return USAGE_SOURCE_LABEL.api
  return USAGE_SOURCE_LABEL[source as UsageSource] ?? source
}

export function fetchUsage(
  token: string,
  params?: {
    from?: string
    to?: string
    model?: string
    source?: UsageSource
    fund_source?: FundSource
  },
) {
  const qs = new URLSearchParams()
  if (params?.from) qs.set('from', params.from)
  if (params?.to) qs.set('to', params.to)
  if (params?.model) qs.set('model', params.model)
  if (params?.source) qs.set('source', params.source)
  if (params?.fund_source) qs.set('fund_source', params.fund_source)
  const q = qs.toString()
  return apiRequest<UsageLog[]>(`/usage${q ? `?${q}` : ''}`, { token })
}

export function fetchUsageDaily(
  token: string,
  params: { from_date: string; to_date: string; model_id?: string },
) {
  const qs = new URLSearchParams({
    from_date: params.from_date,
    to_date: params.to_date,
  })
  if (params.model_id) qs.set('model_id', params.model_id)
  return apiRequest<UserDailyReport>(`/usage/daily?${qs}`, { token })
}
