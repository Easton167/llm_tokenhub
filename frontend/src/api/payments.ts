import { apiRequest } from './client'

export type PaymentOrder = {
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

export type PaymentCreateResponse = {
  order: PaymentOrder
  pay_url: string | null
  external_order_no: string | null
}

export type PaymentCreatePayload = {
  amount: number
  target_type?: 'personal' | 'org'
  org_id?: string
  provider?: 'mock' | 'stripe'
  return_url?: string
}

export type MockCallbackPayload = {
  order_no: string
  external_order_no: string
  success: boolean
  amount: number
  sign: string
  fail_reason?: string
}

export function createPayment(token: string, payload: PaymentCreatePayload) {
  return apiRequest<PaymentCreateResponse>('/payments', { token, body: payload })
}

export function listPayments(
  token: string,
  params?: { status?: string; org_id?: string; limit?: number; offset?: number },
) {
  const qs = new URLSearchParams()
  if (params?.status) qs.set('status', params.status)
  if (params?.org_id) qs.set('org_id', params.org_id)
  if (params?.limit) qs.set('limit', String(params.limit))
  if (params?.offset) qs.set('offset', String(params.offset))
  const q = qs.toString()
  return apiRequest<PaymentOrder[]>(`/payments${q ? `?${q}` : ''}`, { token })
}

export function getPayment(token: string, orderNo: string) {
  return apiRequest<PaymentOrder>(`/payments/${orderNo}`, { token })
}

/** Mock 沙箱回调（无需 JWT；需 HMAC 签名） */
export function mockPaymentCallback(payload: MockCallbackPayload) {
  return apiRequest<PaymentOrder>('/payments/callbacks/mock', {
    method: 'POST',
    body: payload,
  })
}

/** 生成 Mock 回调签名（与后端 mock_sign_payload 一致） */
export async function mockSignPayload(params: {
  order_no: string
  external_order_no: string
  success: boolean
  amount: number
  secret?: string
}): Promise<string> {
  const secret =
    params.secret ||
    (import.meta.env.VITE_MOCK_PAYMENT_SECRET as string | undefined) ||
    'mock-payment-secret'
  const message = `${params.order_no}|${params.external_order_no}|${params.success ? 1 : 0}|${params.amount}`
  const enc = new TextEncoder()
  const key = await crypto.subtle.importKey(
    'raw',
    enc.encode(secret),
    { name: 'HMAC', hash: 'SHA-256' },
    false,
    ['sign'],
  )
  const sig = await crypto.subtle.sign('HMAC', key, enc.encode(message))
  return Array.from(new Uint8Array(sig))
    .map((b) => b.toString(16).padStart(2, '0'))
    .join('')
}
