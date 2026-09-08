import dayjs from 'dayjs'

export const GIFT_EXPIRY_WARN_DAYS = 7

/** 已到期或 remainingDays 日内到期（永久有效返回 false）。 */
export function giftExpiresWithinDays(
  expiresAt: string | null | undefined,
  days = GIFT_EXPIRY_WARN_DAYS,
): boolean {
  if (!expiresAt) return false
  const d = dayjs(expiresAt)
  if (!d.isValid()) return false
  return d.diff(dayjs(), 'day', true) <= days
}

/** 有到期时间的批次里，最近到期的一条；全永久则 null。 */
export function pickNearestExpiringGrant<T extends { expires_at: string | null }>(
  grants: T[],
): T | null {
  let nearest: T | null = null
  let nearestTs = Infinity
  for (const g of grants) {
    if (!g.expires_at) continue
    const ts = dayjs(g.expires_at).valueOf()
    if (!Number.isFinite(ts) || ts >= nearestTs) continue
    nearestTs = ts
    nearest = g
  }
  return nearest
}
