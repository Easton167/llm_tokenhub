import { apiRequest } from './client'

export type User = {
  id: string
  email: string
  nickname: string | null
  status: number
  default_billing_subject: string
  created_at: string
  updated_at: string
}

export type TokenResponse = {
  access_token: string
  token_type: string
}

export function register(email: string, password: string, nickname?: string) {
  return apiRequest<User>('/auth/register', {
    body: { email, password, nickname: nickname || null },
  })
}

export function login(email: string, password: string) {
  return apiRequest<TokenResponse>('/auth/login', {
    body: { email, password },
  })
}

export function fetchMe(token: string) {
  return apiRequest<User>('/auth/me', { token })
}
