export type ApiErrorBody = {
  error?: { message?: string; code?: string; type?: string }
  detail?: unknown
}

export class ApiError extends Error {
  status: number
  code?: string

  constructor(status: number, message: string, code?: string) {
    super(message)
    this.status = status
    this.code = code
  }
}

function formatValidationDetail(detail: unknown): string | null {
  if (typeof detail === 'string' && detail.trim()) return detail
  if (!Array.isArray(detail) || detail.length === 0) return null
  const parts = detail.map((item) => {
    if (!item || typeof item !== 'object') return String(item)
    const row = item as { loc?: unknown[]; msg?: string }
    const loc = Array.isArray(row.loc)
      ? row.loc.filter((x) => x !== 'body').join('.')
      : ''
    const msg = row.msg || 'invalid'
    return loc ? `${loc}: ${msg}` : msg
  })
  return parts.join('; ')
}

async function parseError(res: Response): Promise<ApiError> {
  try {
    const body = (await res.json()) as ApiErrorBody
    const validationMsg = formatValidationDetail(body.detail)
    return new ApiError(
      res.status,
      body.error?.message || validationMsg || res.statusText || 'Request failed',
      body.error?.code,
    )
  } catch {
    return new ApiError(res.status, res.statusText || 'Request failed')
  }
}

type RequestOptions = {
  method?: string
  body?: unknown
  token?: string | null
  apiKey?: string | null
  headers?: Record<string, string>
}

export async function apiRequest<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const headers: Record<string, string> = {
    ...(options.headers || {}),
  }
  if (options.body !== undefined) {
    headers['Content-Type'] = 'application/json'
  }
  const bearer = options.apiKey ?? options.token
  if (bearer) {
    headers.Authorization = `Bearer ${bearer}`
  }

  const res = await fetch(path, {
    method: options.method || (options.body !== undefined ? 'POST' : 'GET'),
    headers,
    body: options.body !== undefined ? JSON.stringify(options.body) : undefined,
  })

  if (!res.ok) {
    throw await parseError(res)
  }
  if (res.status === 204) {
    return undefined as T
  }
  return (await res.json()) as T
}
