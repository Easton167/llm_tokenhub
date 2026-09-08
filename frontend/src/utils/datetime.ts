import dayjs from 'dayjs'

/** 将 API 返回的 ISO 时间格式化为本地可读时间。 */
export function formatDateTime(value: string | null | undefined): string {
  if (!value) return '—'
  const d = dayjs(value)
  return d.isValid() ? d.format('YYYY-MM-DD HH:mm:ss') : value
}
