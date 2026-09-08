import { ApiError, type ApiErrorBody } from './client'

/** strict 未命中时后端固定文案，用于历史消息样式区分 */
export const RAG_MISS_REPLY = '知识库中未找到相关内容'

export type Citation = {
  chunk_id: string
  kb_id: string | null
  document_id: string | null
  document_name: string
  content_summary: string
  score: number
  index: number | null
}

export type ChatMessage = {
  role: 'system' | 'user' | 'assistant'
  content: string
  citations?: Citation[] | null
  /** UI 标记：strict 未命中固定文案 */
  isStrictMiss?: boolean
}

export type ModelItem = {
  id: string
  object: string
  created: number
  owned_by: string
}

export async function listModels(apiKey: string): Promise<ModelItem[]> {
  const res = await fetch('/v1/models', {
    headers: { Authorization: `Bearer ${apiKey}` },
  })
  if (!res.ok) {
    let message = res.statusText
    let code: string | undefined
    try {
      const body = (await res.json()) as ApiErrorBody
      message = body.error?.message || message
      code = body.error?.code
    } catch {
      /* ignore */
    }
    throw new ApiError(res.status, message, code)
  }
  const data = (await res.json()) as { data: ModelItem[] }
  return data.data || []
}

export type StreamHandlers = {
  onDelta: (text: string) => void
  onDone: () => void
  onError: (err: Error) => void
}

export type KbStreamHandlers = StreamHandlers & {
  onCitations?: (
    citations: Citation[],
    rag: {
      hit: boolean
      miss_strategy: string
      used_knowledge_base: boolean
      call_model: boolean
    },
  ) => void
}

async function consumeSseStream(
  res: Response,
  handlers: KbStreamHandlers,
): Promise<void> {
  if (!res.body) {
    throw new Error('No response body')
  }

  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  try {
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      const lines = buffer.split('\n')
      buffer = lines.pop() || ''

      for (const raw of lines) {
        const line = raw.trim()
        if (!line.startsWith('data:')) continue
        const payload = line.slice(5).trim()
        if (payload === '[DONE]') {
          handlers.onDone()
          return
        }
        try {
          const chunk = JSON.parse(payload) as {
            object?: string
            citations?: Citation[]
            rag?: {
              hit: boolean
              miss_strategy: string
              used_knowledge_base: boolean
              call_model: boolean
            }
            choices?: Array<{ delta?: { content?: string }; finish_reason?: string | null }>
          }
          if (chunk.object === 'chat.completion.citations') {
            handlers.onCitations?.(chunk.citations || [], {
              hit: chunk.rag?.hit ?? false,
              miss_strategy: chunk.rag?.miss_strategy ?? 'fallback',
              used_knowledge_base: chunk.rag?.used_knowledge_base ?? false,
              call_model: chunk.rag?.call_model ?? true,
            })
            continue
          }
          const delta = chunk.choices?.[0]?.delta?.content
          if (delta) handlers.onDelta(delta)
        } catch {
          /* skip malformed chunk */
        }
      }
    }
    handlers.onDone()
  } catch (err) {
    if ((err as Error).name === 'AbortError') {
      handlers.onDone()
      return
    }
    handlers.onError(err as Error)
  }
}

export async function streamChatCompletions(
  apiKey: string,
  body: {
    model: string
    messages: ChatMessage[]
    temperature?: number
    max_tokens?: number
  },
  handlers: StreamHandlers,
  signal?: AbortSignal,
): Promise<void> {
  const res = await fetch('/v1/chat/completions', {
    method: 'POST',
    headers: {
      Authorization: `Bearer ${apiKey}`,
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({
      model: body.model,
      messages: body.messages.map((m) => ({ role: m.role, content: m.content })),
      temperature: body.temperature,
      max_tokens: body.max_tokens,
      stream: true,
    }),
    signal,
  })

  if (!res.ok) {
    let message = res.statusText
    let code: string | undefined
    try {
      const errBody = (await res.json()) as ApiErrorBody
      message = errBody.error?.message || message
      code = errBody.error?.code
    } catch {
      /* ignore */
    }
    throw new ApiError(res.status, message, code)
  }

  await consumeSseStream(res, handlers)
}

/** 前台 BFF：挂载知识库时的 RAG 编排流式接口（JWT + X-Api-Key） */
export async function streamKbCompletions(
  token: string,
  apiKey: string,
  body: {
    model: string
    messages: ChatMessage[]
    session_id?: string
    temperature?: number
    max_tokens?: number
    kb_ids?: string[]
    miss_strategy?: 'strict' | 'fallback'
  },
  handlers: KbStreamHandlers,
  signal?: AbortSignal,
): Promise<void> {
  const res = await fetch('/chat/kb-completions', {
    method: 'POST',
    headers: {
      Authorization: `Bearer ${token}`,
      'X-Api-Key': apiKey,
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({
      model: body.model,
      messages: body.messages.map((m) => ({
        role: m.role,
        content: m.content,
      })),
      session_id: body.session_id,
      temperature: body.temperature,
      max_tokens: body.max_tokens,
      kb_ids: body.kb_ids,
      miss_strategy: body.miss_strategy,
      stream: true,
    }),
    signal,
  })

  if (!res.ok) {
    let message = res.statusText
    let code: string | undefined
    try {
      const errBody = (await res.json()) as ApiErrorBody
      message = errBody.error?.message || message
      code = errBody.error?.code
    } catch {
      /* ignore */
    }
    throw new ApiError(res.status, message, code)
  }

  await consumeSseStream(res, handlers)
}

export function isStrictMissMessage(msg: ChatMessage): boolean {
  if (msg.isStrictMiss) return true
  return msg.role === 'assistant' && msg.content === RAG_MISS_REPLY
}

/** 持久化前去掉仅 UI 字段 */
export function toPersistMessages(messages: ChatMessage[]): ChatMessage[] {
  return messages.map((m) => ({
    role: m.role,
    content: m.content,
    citations: m.citations ?? null,
  }))
}
