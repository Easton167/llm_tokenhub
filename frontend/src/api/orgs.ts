import { apiRequest } from './client'
import type { Wallet, WalletTransaction } from './wallet'

export type OrgRole = 'owner' | 'admin' | 'member'
export type BillingFallback = 'personal' | 'deny'
export type QuotaPeriod = 'none' | 'daily' | 'monthly'

export type Org = {
  id: string
  name: string
  owner_user_id: string
  status: number
  billing_fallback: BillingFallback | string
  created_at: string
  updated_at: string
}

export type OrgWithRole = Org & { my_org_role: OrgRole }

export type MemberQuota = {
  quota_period: QuotaPeriod
  quota_limit: number
  quota_used: number
  period_reset_at: string | null
}

export type OrgMember = {
  id: string
  org_id: string
  user_id: string
  org_role: OrgRole
  can_consume_org_wallet: boolean
  status: number
  created_at: string
  updated_at: string
  quota: MemberQuota | null
}

export type OrgDetail = Org & { members: OrgMember[] }

export type DailyMetric = {
  summary_date: string
  request_count: number
  input_tokens: number
  output_tokens: number
  cached_tokens: number
  revenue_amount: number
  cash_revenue_amount?: number
  gift_consumed?: number
  gift_charge_amount?: number
  cash_charge_amount?: number
  cost_amount: number
  gross_profit: number
}

export type OrgDailyReport = {
  timezone: string
  from_date: string
  to_date: string
  org_id: string
  items: DailyMetric[]
}

export type OrgMemberShareItem = {
  user_id: string
  request_count: number
  input_tokens: number
  output_tokens: number
  cached_tokens: number
  revenue_amount: number
  cash_revenue_amount?: number
  gift_consumed?: number
  gift_charge_amount?: number
  cash_charge_amount?: number
  cost_amount: number
  gross_profit: number
}

export type OrgMemberShareReport = {
  timezone: string
  from_date: string
  to_date: string
  org_id: string
  items: OrgMemberShareItem[]
}

export function listOrgs(token: string) {
  return apiRequest<OrgWithRole[]>('/orgs', { token })
}

export function createOrg(
  token: string,
  data: { name: string; billing_fallback?: BillingFallback },
) {
  return apiRequest<Org>('/orgs', { token, body: data })
}

export function getOrg(token: string, orgId: string) {
  return apiRequest<OrgDetail>(`/orgs/${orgId}`, { token })
}

export function updateOrg(
  token: string,
  orgId: string,
  data: { name?: string; billing_fallback?: BillingFallback },
) {
  return apiRequest<Org>(`/orgs/${orgId}`, { method: 'PATCH', token, body: data })
}

export function listOrgMembers(token: string, orgId: string) {
  return apiRequest<OrgMember[]>(`/orgs/${orgId}/members`, { token })
}

export function inviteOrgMember(
  token: string,
  orgId: string,
  data: { user_id: string; org_role?: OrgRole },
) {
  return apiRequest<OrgMember>(`/orgs/${orgId}/members`, { token, body: data })
}

export function updateOrgMember(
  token: string,
  orgId: string,
  memberId: string,
  data: {
    org_role?: OrgRole
    can_consume_org_wallet?: boolean
    quota_period?: QuotaPeriod
    quota_limit?: number
  },
) {
  return apiRequest<OrgMember>(`/orgs/${orgId}/members/${memberId}`, {
    method: 'PATCH',
    token,
    body: data,
  })
}

export function removeOrgMember(token: string, orgId: string, memberId: string) {
  return apiRequest<void>(`/orgs/${orgId}/members/${memberId}`, {
    method: 'DELETE',
    token,
  })
}

export function fetchOrgWallet(token: string, orgId: string) {
  return apiRequest<Wallet>(`/orgs/${orgId}/wallet`, { token })
}

export function fetchOrgTransactions(token: string, orgId: string) {
  return apiRequest<WalletTransaction[]>(`/orgs/${orgId}/wallet/transactions`, { token })
}

export function fetchOrgDailyReport(
  token: string,
  orgId: string,
  params: { from_date: string; to_date: string; model_id?: string },
) {
  const qs = new URLSearchParams({
    from_date: params.from_date,
    to_date: params.to_date,
  })
  if (params.model_id) qs.set('model_id', params.model_id)
  return apiRequest<OrgDailyReport>(`/orgs/${orgId}/reports/daily?${qs}`, { token })
}

export function fetchOrgMemberShareReport(
  token: string,
  orgId: string,
  params: { from_date: string; to_date: string; model_id?: string },
) {
  const qs = new URLSearchParams({
    from_date: params.from_date,
    to_date: params.to_date,
  })
  if (params.model_id) qs.set('model_id', params.model_id)
  return apiRequest<OrgMemberShareReport>(`/orgs/${orgId}/reports/members?${qs}`, {
    token,
  })
}
