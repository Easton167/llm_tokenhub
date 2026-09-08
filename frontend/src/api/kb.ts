import { apiRequest, ApiError, type ApiErrorBody } from './client'

export type KnowledgeBase = {
  id: string
  name: string
  description: string | null
  embedding_model_id: string
  embedding_model: string | null
  embedding_dim: number
  chunk_size: number
  chunk_overlap: number
  separators: string[]
  top_k: number
  score_threshold: number
  doc_count: number
  chunk_count: number
  storage_bytes: number
  status: string
  created_at: string
  updated_at: string
}

export type KnowledgeBaseCreate = {
  name: string
  description?: string | null
  embedding_model?: string | null
  chunk_size?: number | null
  chunk_overlap?: number | null
  separators?: string[] | null
  top_k?: number | null
  score_threshold?: number | null
}

export type KnowledgeBaseUpdate = {
  name?: string
  description?: string | null
  chunk_size?: number | null
  chunk_overlap?: number | null
  separators?: string[] | null
  top_k?: number | null
  score_threshold?: number | null
}

export type KbDocument = {
  id: string
  kb_id: string
  file_name: string
  file_type: string
  file_size_bytes: number
  status: string
  error_message: string | null
  total_tokens: number
  progress: number
  chunk_size: number
  chunk_overlap: number
  separators: string[]
  created_at: string
  updated_at: string
}

export type KbDocumentList = {
  items: KbDocument[]
  total: number
  page: number
  page_size: number
}

export type KbChunk = {
  id: string
  document_id: string
  kb_id: string
  seq: number
  content: string
  token_count: number
  source_location: Record<string, unknown> | null
  is_enabled: boolean
  is_manually_edited: boolean
  embedding_status: string
  created_at: string
  updated_at: string
}

export type KbChunkList = {
  items: KbChunk[]
  total: number
  page: number
  page_size: number
}

export type HitTestHit = {
  chunk_id: string
  document_id: string
  document_name: string
  content: string
  score: number
  seq: number
  source_location: Record<string, unknown> | null
  token_count: number
}

export type HitTestResult = {
  query: string
  top_k: number
  score_threshold: number
  hits: HitTestHit[]
  estimated_charge_amount: number
  request_id: string | null
  input_tokens: number
}

export type RechunkPayload = {
  confirm: boolean
  chunk_size?: number | null
  chunk_overlap?: number | null
  separators?: string[] | null
}

/** 前端预校验默认值（与后端系统默认一致） */
export const KB_UPLOAD_MAX_FILE_BYTES = 20 * 1024 * 1024
export const KB_UPLOAD_BATCH_LIMIT = 10
export const KB_ALLOWED_EXTENSIONS = [
  '.txt',
  '.md',
  '.markdown',
  '.pdf',
  '.docx',
  '.html',
  '.htm',
  '.csv',
  '.xlsx',
] as const

export const KB_DOC_PROCESSING_STATUSES = new Set([
  'uploaded',
  'parsing',
  'chunking',
  'pending_embedding',
  'embedding',
])

export function listKnowledgeBases(token: string) {
  return apiRequest<KnowledgeBase[]>('/kb', { token })
}

export function getKnowledgeBase(token: string, kbId: string) {
  return apiRequest<KnowledgeBase>(`/kb/${kbId}`, { token })
}

export function createKnowledgeBase(token: string, body: KnowledgeBaseCreate) {
  return apiRequest<KnowledgeBase>('/kb', { token, body })
}

export function updateKnowledgeBase(
  token: string,
  kbId: string,
  body: KnowledgeBaseUpdate,
) {
  return apiRequest<KnowledgeBase>(`/kb/${kbId}`, {
    method: 'PATCH',
    token,
    body,
  })
}

export function deleteKnowledgeBase(token: string, kbId: string) {
  return apiRequest<void>(`/kb/${kbId}`, { method: 'DELETE', token })
}

export function listDocuments(
  token: string,
  kbId: string,
  page = 1,
  pageSize = 20,
) {
  const qs = new URLSearchParams({
    page: String(page),
    page_size: String(pageSize),
  })
  return apiRequest<KbDocumentList>(`/kb/${kbId}/documents?${qs}`, { token })
}

export function getDocument(token: string, docId: string) {
  return apiRequest<KbDocument>(`/kb/documents/${docId}`, { token })
}

export function retryDocument(token: string, docId: string) {
  return apiRequest<KbDocument>(`/kb/documents/${docId}/retry`, {
    method: 'POST',
    token,
  })
}

export function deleteDocument(token: string, docId: string) {
  return apiRequest<void>(`/kb/documents/${docId}`, {
    method: 'DELETE',
    token,
  })
}

export function rechunkDocument(
  token: string,
  docId: string,
  body: RechunkPayload,
) {
  return apiRequest<KbDocument>(`/kb/documents/${docId}/rechunk`, {
    method: 'POST',
    token,
    body,
  })
}

export function listChunks(
  token: string,
  docId: string,
  opts: { page?: number; pageSize?: number; q?: string } = {},
) {
  const qs = new URLSearchParams({
    page: String(opts.page ?? 1),
    page_size: String(opts.pageSize ?? 20),
  })
  if (opts.q?.trim()) qs.set('q', opts.q.trim())
  return apiRequest<KbChunkList>(`/kb/documents/${docId}/chunks?${qs}`, {
    token,
  })
}

export function createChunk(
  token: string,
  docId: string,
  body: { content: string; seq?: number | null },
) {
  return apiRequest<KbChunk>(`/kb/documents/${docId}/chunks`, {
    method: 'POST',
    token,
    body,
  })
}

export function updateChunk(
  token: string,
  chunkId: string,
  body: { content?: string; is_enabled?: boolean },
) {
  return apiRequest<KbChunk>(`/kb/chunks/${chunkId}`, {
    method: 'PATCH',
    token,
    body,
  })
}

export function deleteChunk(token: string, chunkId: string) {
  return apiRequest<void>(`/kb/chunks/${chunkId}`, {
    method: 'DELETE',
    token,
  })
}

export function batchChunks(
  token: string,
  body: { chunk_ids: string[]; action: 'enable' | 'disable' | 'delete' },
) {
  return apiRequest<{ action: string; affected: number }>('/kb/chunks/batch', {
    method: 'POST',
    token,
    body,
  })
}

export function hitTest(
  token: string,
  kbId: string,
  body: {
    query: string
    top_k?: number | null
    score_threshold?: number | null
  },
) {
  return apiRequest<HitTestResult>(`/kb/${kbId}/hit-test`, {
    method: 'POST',
    token,
    body,
  })
}

async function parseUploadError(res: Response): Promise<ApiError> {
  try {
    const body = (await res.json()) as ApiErrorBody
    return new ApiError(
      res.status,
      body.error?.message || res.statusText || '上传失败',
      body.error?.code,
    )
  } catch {
    return new ApiError(res.status, res.statusText || '上传失败')
  }
}

/** multipart 批量上传文档 */
export async function uploadDocuments(
  token: string,
  kbId: string,
  files: File[],
): Promise<KbDocument[]> {
  const form = new FormData()
  for (const f of files) {
    form.append('files', f)
  }
  const res = await fetch(`/kb/${kbId}/documents`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${token}` },
    body: form,
  })
  if (!res.ok) {
    throw await parseUploadError(res)
  }
  return (await res.json()) as KbDocument[]
}

export function formatStorageBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KB`
  if (bytes < 1024 ** 3) return `${(bytes / 1024 ** 2).toFixed(2)} MB`
  return `${(bytes / 1024 ** 3).toFixed(2)} GB`
}

export function fileExtension(name: string): string {
  const i = name.lastIndexOf('.')
  return i >= 0 ? name.slice(i).toLowerCase() : ''
}

export function validateUploadFiles(files: File[]): string | null {
  if (files.length === 0) return '请选择文件'
  if (files.length > KB_UPLOAD_BATCH_LIMIT) {
    return `单次最多上传 ${KB_UPLOAD_BATCH_LIMIT} 个文件`
  }
  for (const f of files) {
    const ext = fileExtension(f.name)
    if (!(KB_ALLOWED_EXTENSIONS as readonly string[]).includes(ext)) {
      return `不支持的文件类型：${f.name}（支持 ${KB_ALLOWED_EXTENSIONS.join(' / ')}）`
    }
    if (f.size > KB_UPLOAD_MAX_FILE_BYTES) {
      return `文件过大：${f.name}（上限 ${formatStorageBytes(KB_UPLOAD_MAX_FILE_BYTES)}）`
    }
  }
  return null
}
