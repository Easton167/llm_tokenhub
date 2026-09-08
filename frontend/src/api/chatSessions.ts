import { apiRequest } from './client'
import type { ChatMessage, Citation } from './chat'

export type MissStrategy = 'strict' | 'fallback'

export type ChatSessionSummary = {
  id: string
  title: string
  model: string | null
  temperature: number | null
  max_tokens: number | null
  kb_ids: string[]
  miss_strategy: MissStrategy
  retrieval_top_k: number | null
  retrieval_score_threshold: number | null
  message_count: number
  created_at: string
  updated_at: string
}

export type ChatSessionMessage = ChatMessage & {
  id: string
  seq: number
  created_at: string
  citations?: Citation[] | null
}

export type ChatSessionDetail = {
  id: string
  title: string
  model: string | null
  temperature: number | null
  max_tokens: number | null
  kb_ids: string[]
  miss_strategy: MissStrategy
  retrieval_top_k: number | null
  retrieval_score_threshold: number | null
  messages: ChatSessionMessage[]
  created_at: string
  updated_at: string
}

export type ChatSessionSettings = {
  title?: string
  model?: string | null
  temperature?: number | null
  max_tokens?: number | null
  kb_ids?: string[]
  miss_strategy?: MissStrategy
  retrieval_top_k?: number | null
  retrieval_score_threshold?: number | null
}

export function listChatSessions(token: string) {
  return apiRequest<ChatSessionSummary[]>('/chat/sessions', { token })
}

export function createChatSession(token: string, body: ChatSessionSettings = {}) {
  return apiRequest<ChatSessionDetail>('/chat/sessions', { token, body })
}

export function getChatSession(token: string, sessionId: string) {
  return apiRequest<ChatSessionDetail>(`/chat/sessions/${sessionId}`, { token })
}

export function updateChatSession(
  token: string,
  sessionId: string,
  body: ChatSessionSettings,
) {
  return apiRequest<ChatSessionDetail>(`/chat/sessions/${sessionId}`, {
    method: 'PATCH',
    token,
    body,
  })
}

export function deleteChatSession(token: string, sessionId: string) {
  return apiRequest<void>(`/chat/sessions/${sessionId}`, { method: 'DELETE', token })
}

export function replaceChatMessages(
  token: string,
  sessionId: string,
  body: {
    messages: ChatMessage[]
    title?: string
    model?: string | null
    temperature?: number | null
    max_tokens?: number | null
  },
) {
  return apiRequest<ChatSessionDetail>(`/chat/sessions/${sessionId}/messages`, {
    method: 'PUT',
    token,
    body,
  })
}
