import type { ReactNode } from 'react'
import { Tag } from 'antd'

export const KB_STATUS_MAP: Record<string, { label: string; color: string }> = {
  active: { label: '正常', color: 'success' },
  disabled: { label: '已禁用', color: 'default' },
}

export const DOC_STATUS_MAP: Record<string, { label: string; color: string }> = {
  uploaded: { label: '已上传', color: 'default' },
  parsing: { label: '解析中', color: 'processing' },
  chunking: { label: '分块中', color: 'processing' },
  pending_embedding: { label: '待向量化', color: 'warning' },
  insufficient_balance: { label: '余额不足', color: 'error' },
  embedding: { label: '向量化中', color: 'processing' },
  ready: { label: '就绪', color: 'success' },
  partial: { label: '部分成功', color: 'warning' },
  failed: { label: '失败', color: 'error' },
}

export const CHUNK_EMBED_MAP: Record<string, { label: string; color: string }> = {
  pending: { label: '待向量化', color: 'default' },
  success: { label: '已向量化', color: 'success' },
  failed: { label: '向量化失败', color: 'error' },
}

export function StatusTag({
  map,
  value,
}: {
  map: Record<string, { label: string; color: string }>
  value: string
}) {
  const meta = map[value] ?? { label: value, color: 'default' }
  return <Tag color={meta.color}>{meta.label}</Tag>
}

/** 在文本中高亮 query 关键词（简单分词，忽略大小写） */
export function highlightQuery(text: string, query: string): ReactNode {
  const terms = query
    .trim()
    .split(/\s+/)
    .filter((t) => t.length > 0)
    .slice(0, 8)
  if (terms.length === 0) return text
  const escaped = terms.map((t) => t.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'))
  const re = new RegExp(`(${escaped.join('|')})`, 'gi')
  const parts = text.split(re)
  const termSet = new Set(terms.map((t) => t.toLowerCase()))
  return parts.map((part, i) =>
    termSet.has(part.toLowerCase()) ? (
      <mark key={i} style={{ background: '#fef08a', padding: 0 }}>
        {part}
      </mark>
    ) : (
      <span key={i}>{part}</span>
    ),
  )
}
