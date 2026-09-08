/**
 * Admin API 封装
 * 所有接口均使用 JWT token（管理员登录后获得）
 */
import { apiRequest } from './client'

// ---- Types ----

export type Provider = {
  id: string
  name: string
  adapter_type: string
  default_base_url: string | null
  created_at: string
  updated_at: string
}

export type Channel = {
  id: string
  provider_id: string
  name: string
  base_url: string
  adapter_type: string
  priority: number
  weight: number
  status: number
  health_status: number
  fail_count: number
  timeout_ms: number
  max_retries: number
  created_at: string
  updated_at: string
}

export type ChannelKey = {
  id: string
  channel_id: string
  key_masked: string
  weight: number
  rpm_limit: number | null
  tpm_limit: number | null
  status: number
  health_status: number
  created_at: string
  updated_at: string
}

export type LLMModel = {
  id: string
  alias: string
  display_name: string
  model_type: string
  capabilities: Record<string, unknown>
  visibility: string
  status: number
  created_at: string
  updated_at: string
}

export type ModelMapping = {
  id: string
  model_id: string
  channel_id: string
  upstream_model_name: string
  weight: number
  status: number
  created_at: string
  updated_at: string
}

export type BillingRule = {
  id: string
  model_id: string
  version: number
  price_input: number
  price_output: number
  price_cached_input: number
  price_per_call: number
  cost_input: number
  cost_output: number
  multiplier: string
  min_charge_unit: number
  currency: string
  effective_at: string
  status: number
  created_at: string
  updated_at: string
}

export type Role = {
  id: string
  name: string
  description: string | null
  kb_max_kb_count: number | null
  kb_max_docs_per_kb: number | null
  kb_max_storage_bytes: number | null
  kb_max_file_size_bytes: number | null
  created_at: string
  updated_at: string
}

export type KbGlobalSettings = {
  default_embedding_model: string | null
  max_file_size_bytes: number | null
  upload_batch_limit: number | null
  embedding_batch_size: number | null
  embedding_max_concurrent: number | null
  effective_default_embedding_model: string
  effective_max_file_size_bytes: number
  effective_upload_batch_limit: number
  effective_embedding_batch_size: number
  effective_embedding_max_concurrent: number
}

export type AdminKnowledgeBase = {
  id: string
  owner_user_id: string
  owner_email: string | null
  name: string
  description: string | null
  embedding_model_id: string
  embedding_model_alias: string | null
  embedding_dim: number
  doc_count: number
  chunk_count: number
  storage_bytes: number
  status: string
  created_at: string
  updated_at: string
}

export type AdminKbUserStats = {
  user_id: string
  email: string | null
  kb_count: number
  doc_count: number
  storage_bytes: number
  embed_input_tokens: number
}

export type RoleModelGrant = {
  role_id: string
  model_id: string
  created_at: string
  updated_at: string
}

export type UserRoleOut = {
  user_id: string
  role_id: string
  created_at: string
  updated_at: string
}

export type AdminUser = {
  id: string
  email: string
  nickname: string | null
  status: number
  default_billing_subject: string
  created_at: string
  updated_at: string
}

export type WalletOut = {
  id: string
  owner_type: string
  owner_id: string
  balance: number
  held: number
  gift_balance: number
  gift_held: number
  total_available: number
  currency: string
  credit_limit: number
  created_at: string
  updated_at: string
}

export type WalletTransaction = {
  id: string
  wallet_id: string
  type: string
  amount: number
  gift_amount?: number
  balance_after: number
  held_after: number
  ref_request_id: string | null
  operator_user_id: string | null
  remark: string | null
  created_at: string
}

export type GiftReason = 'promotion' | 'compensation' | 'internal' | 'partnership' | 'other'

export type GiftGrantCreate = {
  amount: number
  expires_in_days?: number
  expires_at?: string
  never_expires?: boolean
  reason: GiftReason
  remark?: string
  idempotency_key: string
}

export type GiftGrantOut = {
  id: string
  wallet_id: string
  amount: number
  remaining: number
  reason: string
  expires_at: string | null
  status: string
  granted_by: string
  revoked_by: string | null
  revoked_at: string | null
  idempotency_key: string
  remark: string | null
  created_at: string
  updated_at: string
}

export type RequestLog = {
  id: string
  request_id: string
  api_key_id: string | null
  user_id: string
  billing_subject: string
  model_id: string
  channel_id: string | null
  status: string
  http_status: number
  latency_ms: number | null
  is_stream: boolean
  error_code: string | null
  created_at: string
}

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
  is_estimated: boolean
  created_at: string
}

export type UsageSummary = {
  model_id: string | null
  user_id: string | null
  request_count: number
  input_tokens: number
  output_tokens: number
  cached_tokens: number
  cost_amount: number
  charge_amount: number
}

// ---- Provider ----

export const adminApi = {
  // Provider
  listProviders: (token: string) =>
    apiRequest<Provider[]>('/admin/providers', { token }),

  createProvider: (token: string, data: { name: string; adapter_type: string; default_base_url?: string }) =>
    apiRequest<Provider>('/admin/providers', { token, body: data }),

  updateProvider: (token: string, id: string, data: Partial<{ name: string; adapter_type: string; default_base_url: string }>) =>
    apiRequest<Provider>(`/admin/providers/${id}`, { token, method: 'PATCH', body: data }),

  deleteProvider: (token: string, id: string) =>
    apiRequest<void>(`/admin/providers/${id}`, { token, method: 'DELETE' }),

  // Channel
  listChannels: (token: string) =>
    apiRequest<Channel[]>('/admin/channels', { token }),

  createChannel: (
    token: string,
    data: {
      provider_id: string
      name: string
      base_url?: string
      adapter_type?: string
      priority?: number
      weight?: number
      timeout_ms?: number
      max_retries?: number
      status?: number
    },
  ) => apiRequest<Channel>('/admin/channels', { method: 'POST', token, body: data }),

  updateChannel: (token: string, id: string, data: object) =>
    apiRequest<Channel>(`/admin/channels/${id}`, { token, method: 'PATCH', body: data }),

  deleteChannel: (token: string, id: string) =>
    apiRequest<void>(`/admin/channels/${id}`, { token, method: 'DELETE' }),

  // Channel Key
  listChannelKeys: (token: string, channelId: string) =>
    apiRequest<ChannelKey[]>(`/admin/channels/${channelId}/keys`, { token }),

  createChannelKey: (token: string, channelId: string, data: { api_key: string; weight?: number; rpm_limit?: number; tpm_limit?: number }) =>
    apiRequest<ChannelKey>(`/admin/channels/${channelId}/keys`, { token, body: data }),

  updateChannelKey: (token: string, channelId: string, keyId: string, data: object) =>
    apiRequest<ChannelKey>(`/admin/channels/${channelId}/keys/${keyId}`, { token, method: 'PATCH', body: data }),

  deleteChannelKey: (token: string, channelId: string, keyId: string) =>
    apiRequest<void>(`/admin/channels/${channelId}/keys/${keyId}`, { token, method: 'DELETE' }),

  // Model
  listModels: (token: string) =>
    apiRequest<LLMModel[]>('/admin/models', { token }),

  createModel: (token: string, data: object) =>
    apiRequest<LLMModel>('/admin/models', { token, body: data }),

  updateModel: (token: string, id: string, data: object) =>
    apiRequest<LLMModel>(`/admin/models/${id}`, { token, method: 'PATCH', body: data }),

  deleteModel: (token: string, id: string) =>
    apiRequest<void>(`/admin/models/${id}`, { token, method: 'DELETE' }),

  // Model Mapping
  listModelMappings: (token: string, modelId: string) =>
    apiRequest<ModelMapping[]>(`/admin/models/${modelId}/mappings`, { token }),

  createModelMapping: (token: string, modelId: string, data: { channel_id: string; upstream_model_name: string; weight?: number; status?: number }) =>
    apiRequest<ModelMapping>(`/admin/models/${modelId}/mappings`, { token, body: data }),

  updateModelMapping: (token: string, modelId: string, mappingId: string, data: object) =>
    apiRequest<ModelMapping>(`/admin/models/${modelId}/mappings/${mappingId}`, { token, method: 'PATCH', body: data }),

  deleteModelMapping: (token: string, modelId: string, mappingId: string) =>
    apiRequest<void>(`/admin/models/${modelId}/mappings/${mappingId}`, { token, method: 'DELETE' }),

  // Billing Rule
  listBillingRules: (token: string, modelId: string) =>
    apiRequest<BillingRule[]>(`/admin/models/${modelId}/billing-rules`, { token }),

  createBillingRule: (token: string, modelId: string, data: object) =>
    apiRequest<BillingRule>(`/admin/models/${modelId}/billing-rules`, { token, body: data }),

  deleteBillingRule: (token: string, modelId: string, ruleId: string) =>
    apiRequest<void>(`/admin/models/${modelId}/billing-rules/${ruleId}`, { token, method: 'DELETE' }),

  // Role
  listRoles: (token: string) =>
    apiRequest<Role[]>('/admin/roles', { token }),

  createRole: (token: string, data: {
    name: string
    description?: string
    kb_max_kb_count?: number | null
    kb_max_docs_per_kb?: number | null
    kb_max_storage_bytes?: number | null
    kb_max_file_size_bytes?: number | null
  }) =>
    apiRequest<Role>('/admin/roles', { token, body: data }),

  updateRole: (token: string, id: string, data: object) =>
    apiRequest<Role>(`/admin/roles/${id}`, { token, method: 'PATCH', body: data }),

  deleteRole: (token: string, id: string) =>
    apiRequest<void>(`/admin/roles/${id}`, { token, method: 'DELETE' }),

  listRoleModelGrants: (token: string, roleId: string) =>
    apiRequest<RoleModelGrant[]>(`/admin/roles/${roleId}/models`, { token }),

  grantModelToRole: (token: string, roleId: string, modelId: string) =>
    apiRequest<RoleModelGrant>(`/admin/roles/${roleId}/models`, { token, body: { model_id: modelId } }),

  revokeModelFromRole: (token: string, roleId: string, modelId: string) =>
    apiRequest<void>(`/admin/roles/${roleId}/models/${modelId}`, { token, method: 'DELETE' }),

  // Knowledge base admin
  listAdminKbs: (token: string, params?: { owner_user_id?: string; status?: string }) => {
    const q = new URLSearchParams()
    if (params?.owner_user_id) q.set('owner_user_id', params.owner_user_id)
    if (params?.status) q.set('status', params.status)
    const qs = q.toString()
    return apiRequest<AdminKnowledgeBase[]>(`/admin/kb${qs ? `?${qs}` : ''}`, { token })
  },

  disableAdminKb: (token: string, id: string) =>
    apiRequest<AdminKnowledgeBase>(`/admin/kb/${id}/disable`, { token, method: 'POST' }),

  enableAdminKb: (token: string, id: string) =>
    apiRequest<AdminKnowledgeBase>(`/admin/kb/${id}/enable`, { token, method: 'POST' }),

  listKbUserStats: (token: string, userId?: string) => {
    const q = userId ? `?user_id=${encodeURIComponent(userId)}` : ''
    return apiRequest<AdminKbUserStats[]>(`/admin/kb/user-stats${q}`, { token })
  },

  getKbSettings: (token: string) =>
    apiRequest<KbGlobalSettings>('/admin/kb/settings', { token }),

  updateKbSettings: (token: string, data: object) =>
    apiRequest<KbGlobalSettings>('/admin/kb/settings', { token, method: 'PATCH', body: data }),

  // User Roles
  listUsers: (token: string) =>
    apiRequest<AdminUser[]>('/admin/users', { token }),

  resetUserPassword: (token: string, userId: string, password: string) =>
    apiRequest<void>(`/admin/users/${userId}/password`, { token, method: 'PUT', body: { password } }),

  listUserRoles: (token: string, userId: string) =>
    apiRequest<UserRoleOut[]>(`/admin/users/${userId}/roles`, { token }),

  assignRoleToUser: (token: string, userId: string, roleId: string) =>
    apiRequest<UserRoleOut>(`/admin/users/${userId}/roles`, { token, body: { role_id: roleId } }),

  removeRoleFromUser: (token: string, userId: string, roleId: string) =>
    apiRequest<void>(`/admin/users/${userId}/roles/${roleId}`, { token, method: 'DELETE' }),

  // Wallet
  getUserWallet: (token: string, userId: string) =>
    apiRequest<WalletOut>(`/admin/wallets/users/${userId}`, { token }),

  rechargeWallet: (token: string, userId: string, amount: number, remark?: string) =>
    apiRequest<WalletOut>(`/admin/wallets/users/${userId}/recharge`, { token, body: { amount, remark } }),

  adjustWallet: (token: string, userId: string, amount: number, remark?: string) =>
    apiRequest<WalletOut>(`/admin/wallets/users/${userId}/adjust`, { token, body: { amount, remark } }),

  listUserTransactions: (token: string, userId: string, params?: { limit?: number; offset?: number }) => {
    const q = new URLSearchParams()
    if (params?.limit) q.set('limit', String(params.limit))
    if (params?.offset) q.set('offset', String(params.offset))
    const qs = q.toString()
    return apiRequest<WalletTransaction[]>(`/admin/wallets/users/${userId}/transactions${qs ? '?' + qs : ''}`, { token })
  },

  grantUserGift: (token: string, userId: string, data: GiftGrantCreate) =>
    apiRequest<GiftGrantOut>(`/admin/wallets/users/${userId}/gifts`, { token, body: data }),

  grantOrgGift: (token: string, orgId: string, data: GiftGrantCreate) =>
    apiRequest<GiftGrantOut>(`/admin/wallets/orgs/${orgId}/gifts`, { token, body: data }),

  listWalletGifts: (
    token: string,
    walletId: string,
    params?: { status?: string; limit?: number; offset?: number },
  ) => {
    const q = new URLSearchParams()
    if (params?.status) q.set('status', params.status)
    if (params?.limit) q.set('limit', String(params.limit))
    if (params?.offset) q.set('offset', String(params.offset))
    const qs = q.toString()
    return apiRequest<GiftGrantOut[]>(`/admin/wallets/${walletId}/gifts${qs ? '?' + qs : ''}`, { token })
  },

  revokeGift: (token: string, grantId: string, remark?: string) =>
    apiRequest<GiftGrantOut>(`/admin/gifts/${grantId}/revoke`, { token, body: { remark } }),

  // Usage
  listRequestLogs: (token: string, params?: { limit?: number; offset?: number; status?: string }) => {
    const q = new URLSearchParams()
    if (params?.limit) q.set('limit', String(params.limit))
    if (params?.offset) q.set('offset', String(params.offset))
    if (params?.status) q.set('status', params.status)
    const qs = q.toString()
    return apiRequest<RequestLog[]>(`/admin/request-logs${qs ? '?' + qs : ''}`, { token })
  },

  getUsageSummary: (token: string, params?: { group_by?: 'model' | 'user'; from?: string; to?: string }) => {
    const q = new URLSearchParams()
    if (params?.group_by) q.set('group_by', params.group_by)
    if (params?.from) q.set('from', params.from)
    if (params?.to) q.set('to', params.to)
    const qs = q.toString()
    return apiRequest<UsageSummary[]>(`/admin/usage-summary${qs ? '?' + qs : ''}`, { token })
  },

  // ---- Org ----
  listOrgs: (token: string, params?: { status?: number; limit?: number; offset?: number }) => {
    const q = new URLSearchParams()
    if (params?.status != null) q.set('status', String(params.status))
    if (params?.limit) q.set('limit', String(params.limit))
    if (params?.offset) q.set('offset', String(params.offset))
    const qs = q.toString()
    return apiRequest<AdminOrg[]>(`/admin/orgs${qs ? '?' + qs : ''}`, { token })
  },

  getOrg: (token: string, orgId: string) =>
    apiRequest<AdminOrgDetail>(`/admin/orgs/${orgId}`, { token }),

  deactivateOrg: (token: string, orgId: string, reason?: string) =>
    apiRequest<AdminOrg>(`/admin/orgs/${orgId}/deactivate`, {
      token,
      method: 'POST',
      body: { reason },
    }),

  getOrgWallet: (token: string, orgId: string) =>
    apiRequest<WalletOut>(`/admin/wallets/orgs/${orgId}`, { token }),

  rechargeOrgWallet: (token: string, orgId: string, amount: number, remark?: string) =>
    apiRequest<WalletOut>(`/admin/wallets/orgs/${orgId}/recharge`, {
      token,
      body: { amount, remark },
    }),

  // ---- Payments ----
  listPayments: (
    token: string,
    params?: {
      user_id?: string
      org_id?: string
      status?: string
      provider?: string
      limit?: number
      offset?: number
    },
  ) => {
    const q = new URLSearchParams()
    if (params?.user_id) q.set('user_id', params.user_id)
    if (params?.org_id) q.set('org_id', params.org_id)
    if (params?.status) q.set('status', params.status)
    if (params?.provider) q.set('provider', params.provider)
    if (params?.limit) q.set('limit', String(params.limit))
    if (params?.offset) q.set('offset', String(params.offset))
    const qs = q.toString()
    return apiRequest<AdminPaymentOrder[]>(`/admin/payments${qs ? '?' + qs : ''}`, { token })
  },

  getPayment: (token: string, orderNo: string) =>
    apiRequest<AdminPaymentOrder>(`/admin/payments/${orderNo}`, { token }),

  // ---- Reports ----
  reportTrend: (token: string, params: ReportDateParams) => {
    const q = buildReportQuery(params)
    return apiRequest<TrendReport>(`/admin/reports/trend?${q}`, { token })
  },

  reportTop: (
    token: string,
    params: ReportDateParams & { dimension: 'user' | 'org' | 'model'; limit?: number },
  ) => {
    const q = buildReportQuery(params)
    q.set('dimension', params.dimension)
    if (params.limit) q.set('limit', String(params.limit))
    return apiRequest<TopReport>(`/admin/reports/top?${q}`, { token })
  },

  reportChannelShare: (token: string, params: ReportDateParams) => {
    const q = buildReportQuery(params)
    return apiRequest<ChannelShareReport>(`/admin/reports/channel-share?${q}`, { token })
  },

  reportReconcile: (token: string, params: ReportDateParams) => {
    const q = buildReportQuery(params)
    return apiRequest<ReconcileReport>(`/admin/reports/reconcile?${q}`, { token })
  },

  reportGifts: (token: string, params: ReportDateParams) => {
    const q = buildReportQuery(params)
    return apiRequest<GiftDashboard>(`/admin/reports/gifts?${q}`, { token })
  },

  // 后台 RBAC：账号、后台角色、权限目录
  listPermissionGroups: (token: string) =>
    apiRequest<PermissionGroup[]>('/admin/permissions', { token }),

  listAdminRoles: (token: string) =>
    apiRequest<AdminRoleDef[]>('/admin/admin-roles', { token }),

  createAdminRole: (token: string, data: AdminRolePayload) =>
    apiRequest<AdminRoleDef>('/admin/admin-roles', { token, body: data }),

  updateAdminRole: (token: string, id: string, data: Partial<AdminRolePayload>) =>
    apiRequest<AdminRoleDef>(`/admin/admin-roles/${id}`, { token, method: 'PATCH', body: data }),

  deleteAdminRole: (token: string, id: string) =>
    apiRequest<void>(`/admin/admin-roles/${id}`, { token, method: 'DELETE' }),

  listAdminAccounts: (token: string) =>
    apiRequest<AdminAccount[]>('/admin/accounts', { token }),

  createAdminAccount: (
    token: string,
    data: {
      username: string
      password: string
      display_name?: string | null
      is_superadmin?: boolean
      role_ids?: string[]
    },
  ) => apiRequest<AdminAccount>('/admin/accounts', { token, body: data }),

  updateAdminAccount: (
    token: string,
    id: string,
    data: Partial<{
      display_name: string | null
      status: number
      is_superadmin: boolean
      role_ids: string[]
    }>,
  ) => apiRequest<AdminAccount>(`/admin/accounts/${id}`, { token, method: 'PATCH', body: data }),

  resetAdminAccountPassword: (token: string, id: string, password: string) =>
    apiRequest<void>(`/admin/accounts/${id}/password`, {
      token,
      method: 'PUT',
      body: { password },
    }),

  deleteAdminAccount: (token: string, id: string) =>
    apiRequest<void>(`/admin/accounts/${id}`, { token, method: 'DELETE' }),
}

export type AdminOrg = {
  id: string
  name: string
  owner_user_id: string
  status: number
  billing_fallback: string
  created_at: string
  updated_at: string
}

export type AdminOrgMember = {
  id: string
  org_id: string
  user_id: string
  org_role: string
  can_consume_org_wallet: boolean
  status: number
  created_at: string
  updated_at: string
  quota: {
    quota_period: string
    quota_limit: number
    quota_used: number
    period_reset_at: string | null
  } | null
}

export type AdminOrgDetail = AdminOrg & { members: AdminOrgMember[] }

export type AdminPaymentOrder = {
  id: string
  order_no: string
  user_id: string
  target_wallet_id: string
  target_type: string
  org_id: string | null
  amount: number
  currency: string
  provider: string
  status: string
  external_order_no: string | null
  paid_at: string | null
  expired_at: string | null
  fail_reason: string | null
  created_at: string
  updated_at: string
}

export type ReportDateParams = {
  from_date: string
  to_date: string
  org_id?: string
  user_id?: string
  model_id?: string
  channel_id?: string
}

export type DailyMetric = {
  summary_date: string
  request_count: number
  input_tokens: number
  output_tokens: number
  cached_tokens: number
  revenue_amount: number
  cash_revenue_amount: number
  gift_consumed: number
  gift_cost: number
  cost_amount: number
  gross_profit: number
}

export type TrendReport = {
  timezone: string
  from_date: string
  to_date: string
  items: DailyMetric[]
}

export type TopItem = {
  dim_id: string | null
  request_count: number
  input_tokens: number
  output_tokens: number
  cached_tokens: number
  revenue_amount: number
  cash_revenue_amount?: number
  gift_consumed?: number
  gift_cost?: number
  cost_amount: number
  gross_profit: number
}

export type TopReport = {
  timezone: string
  from_date: string
  to_date: string
  dimension: string
  items: TopItem[]
}

export type ChannelShareItem = {
  channel_id: string | null
  request_count: number
  revenue_amount: number
  cash_revenue_amount?: number
  gift_consumed?: number
  gift_cost?: number
  cost_amount: number
  gross_profit: number
  cost_share_bps: number
}

export type ChannelShareReport = {
  timezone: string
  from_date: string
  to_date: string
  total_cost: number
  items: ChannelShareItem[]
}

export type ReconcileReport = {
  timezone: string
  from_date: string
  to_date: string
  summary_revenue: number
  capture_revenue: number
  matched: boolean
  usage_gift_total?: number
  capture_gift_total?: number
  gift_matched?: boolean
  alerts?: string[]
}

export type GiftDashboard = {
  timezone: string
  from_date: string
  to_date: string
  gift_balance_total: number
  granted_amount: number
  revoked_amount: number
  expired_amount: number
  gift_consumed: number
  gift_cost: number
  granted_all_time: number
  consumed_all_time: number
  redemption_rate_bps: number
}

// ---- 后台 RBAC ----

/** 权限码目录由后端从代码常量派生，前端只负责渲染勾选框 */
export type PermissionGroup = {
  resource: string
  name: string
  permissions: { code: string; action: string; name: string }[]
}

export type AdminRoleDef = {
  id: string
  code: string
  name: string
  description: string | null
  data_scope: string
  permissions: string[]
  created_at: string
  updated_at: string
}

export type AdminRolePayload = {
  code: string
  name: string
  description?: string | null
  data_scope?: string
  permissions?: string[]
}

/** 后台管理员账号，与前台 users 表无关 */
export type AdminAccount = {
  id: string
  username: string
  display_name: string | null
  status: number
  is_superadmin: boolean
  role_ids: string[]
  created_at: string
  updated_at: string
}

function buildReportQuery(params: ReportDateParams): URLSearchParams {
  const q = new URLSearchParams({
    from_date: params.from_date,
    to_date: params.to_date,
  })
  if (params.org_id) q.set('org_id', params.org_id)
  if (params.user_id) q.set('user_id', params.user_id)
  if (params.model_id) q.set('model_id', params.model_id)
  if (params.channel_id) q.set('channel_id', params.channel_id)
  return q
}
