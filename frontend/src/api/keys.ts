import { apiRequest } from './client'

export type ApiKeyItem = {
  id: string
  name: string
  prefix: string
  status: number
  billing_subject: string | null
  allowed_models: string[]
  rpm_limit: number | null
  tpm_limit: number | null
  expires_at: string | null
  last_used_at: string | null
  created_at: string
  updated_at: string
}

export type ApiKeyCreated = ApiKeyItem & { key: string }

export type ApiKeyCreatePayload = {
  name: string
  billing_subject?: string | null
  allowed_models?: string[]
  rpm_limit?: number | null
  tpm_limit?: number | null
  expires_at?: string | null
}

export type ApiKeyUpdatePayload = {
  name?: string
  billing_subject?: string | null
  allowed_models?: string[]
  rpm_limit?: number | null
  tpm_limit?: number | null
  expires_at?: string | null
}

export function listKeys(token: string) {
  return apiRequest<ApiKeyItem[]>('/keys', { token })
}

export function createKey(token: string, payload: ApiKeyCreatePayload | string) {
  const body =
    typeof payload === 'string' ? { name: payload } : payload
  return apiRequest<ApiKeyCreated>('/keys', { token, body })
}

export function updateKey(token: string, id: string, payload: ApiKeyUpdatePayload) {
  return apiRequest<ApiKeyItem>(`/keys/${id}`, {
    method: 'PATCH',
    token,
    body: payload,
  })
}

export function deleteKey(token: string, id: string) {
  return apiRequest<void>(`/keys/${id}`, { method: 'DELETE', token })
}
