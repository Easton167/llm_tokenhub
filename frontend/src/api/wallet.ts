import { apiRequest } from './client'

/** 钱包：balance/held 仅为现金池；总可用额看 total_available。 */
export type Wallet = {
  id: string
  owner_type: string
  owner_id: string
  balance: number
  held: number
  gift_balance?: number
  gift_held?: number
  total_available?: number
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

/** 前台只读赠送批次，不暴露操作人。 */
export type GiftGrantPublic = {
  id: string
  amount: number
  remaining: number
  reason: string
  expires_at: string | null
  status: string
  created_at: string
}

export const TX_TYPE_MAP: Record<string, { text: string; color: string }> = {
  hold: { text: '预扣', color: 'orange' },
  capture: { text: '实扣', color: 'red' },
  release: { text: '释放', color: 'blue' },
  recharge: { text: '充值', color: 'green' },
  adjust: { text: '调账', color: 'purple' },
  gift_grant: { text: '赠送', color: 'cyan' },
  gift_revoke: { text: '撤回赠金', color: 'gold' },
  gift_expire: { text: '赠金过期', color: 'default' },
}

export function formatTxType(type: string): { text: string; color: string } {
  return TX_TYPE_MAP[type] ?? { text: type, color: 'default' }
}

/** 402 / 前端可否调用：赠送 + 充值 + 授信。 */
export function walletTotalAvailable(w: Wallet | null | undefined): number {
  if (!w) return 0
  if (typeof w.total_available === 'number') return w.total_available
  return (w.gift_balance ?? 0) + (w.balance ?? 0) + (w.credit_limit ?? 0)
}

export function fetchWallet(token: string) {
  return apiRequest<Wallet>('/wallets/me', { token })
}

export function fetchTransactions(token: string) {
  return apiRequest<WalletTransaction[]>('/wallets/me/transactions', { token })
}

export function fetchGifts(token: string) {
  return apiRequest<GiftGrantPublic[]>('/wallets/me/gifts', { token })
}
