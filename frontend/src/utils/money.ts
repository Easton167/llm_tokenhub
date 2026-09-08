/** 默认 money_scale=10000，展示为元 */
export function formatMoney(units: number): string {
  return (units / 10_000).toFixed(4)
}

/** 元 → 最小货币单位 */
export function yuanToUnits(yuan: number): number {
  return Math.round(yuan * 10_000)
}
