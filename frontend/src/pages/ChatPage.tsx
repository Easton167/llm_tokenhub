import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  Alert,
  Button,
  Card,
  Collapse,
  Input,
  InputNumber,
  Select,
  Space,
  Switch,
  Typography,
  message,
  Empty,
  List,
  Spin,
} from 'antd'
import { PlusOutlined } from '@ant-design/icons'
import { Link, useNavigate } from 'react-router-dom'
import {
  isStrictMissMessage,
  listModels,
  streamChatCompletions,
  streamKbCompletions,
  toPersistMessages,
  type ChatMessage,
  type Citation,
  type ModelItem,
} from '../api/chat'
import {
  createChatSession,
  deleteChatSession,
  getChatSession,
  listChatSessions,
  replaceChatMessages,
  updateChatSession,
  type ChatSessionDetail,
  type ChatSessionSummary,
  type MissStrategy,
} from '../api/chatSessions'
import { listKnowledgeBases, type KnowledgeBase } from '../api/kb'
import { ApiError } from '../api/client'
import { useAuth } from '../auth/AuthContext'

const LOCAL_SESSIONS_KEY = 'tokenhub_chat_sessions'
const LOCAL_MIGRATED_KEY = 'tokenhub_chat_sessions_migrated'

type LocalSession = {
  id: string
  title: string
  messages: ChatMessage[]
  updatedAt: number
}

type UiSession = {
  id: string
  title: string
  messages: ChatMessage[]
  model?: string | null
  temperature?: number | null
  max_tokens?: number | null
  kb_ids: string[]
  miss_strategy: MissStrategy
  updatedAt: string
  loaded: boolean
}

function readLocalSessions(): LocalSession[] {
  try {
    const raw = localStorage.getItem(LOCAL_SESSIONS_KEY)
    if (!raw) return []
    return JSON.parse(raw) as LocalSession[]
  } catch {
    return []
  }
}

function mapMessages(detail: ChatSessionDetail): ChatMessage[] {
  return detail.messages.map((m) => ({
    role: m.role,
    content: m.content,
    citations: m.citations ?? null,
    isStrictMiss: isStrictMissMessage({
      role: m.role,
      content: m.content,
      citations: m.citations,
    }),
  }))
}

function toUiSession(detail: ChatSessionDetail): UiSession {
  return {
    id: detail.id,
    title: detail.title,
    messages: mapMessages(detail),
    model: detail.model,
    temperature: detail.temperature,
    max_tokens: detail.max_tokens,
    kb_ids: detail.kb_ids || [],
    miss_strategy: detail.miss_strategy || 'fallback',
    updatedAt: detail.updated_at,
    loaded: true,
  }
}

function summaryToUi(summary: ChatSessionSummary): UiSession {
  return {
    id: summary.id,
    title: summary.title,
    messages: [],
    model: summary.model,
    temperature: summary.temperature,
    max_tokens: summary.max_tokens,
    kb_ids: summary.kb_ids || [],
    miss_strategy: summary.miss_strategy || 'fallback',
    updatedAt: summary.updated_at,
    loaded: summary.message_count === 0,
  }
}

function CitationList({ citations }: { citations: Citation[] }) {
  const navigate = useNavigate()
  if (!citations.length) return null

  return (
    <Collapse
      size="small"
      className="chat-citations"
      items={[
        {
          key: 'citations',
          label: `引用来源（${citations.length}）`,
          children: (
            <List
              size="small"
              dataSource={citations}
              renderItem={(c, i) => {
                const canJump = Boolean(c.kb_id && c.document_id && c.chunk_id)
                return (
                  <List.Item
                    style={{ cursor: canJump ? 'pointer' : 'default', padding: '8px 0' }}
                    onClick={() => {
                      if (!canJump) return
                      navigate(
                        `/kb/${c.kb_id}/documents/${c.document_id}?chunk=${c.chunk_id}`,
                      )
                    }}
                  >
                    <Space direction="vertical" size={2} style={{ width: '100%' }}>
                      <Space wrap>
                        <Typography.Text strong>
                          [{c.index ?? i + 1}] {c.document_name || '未命名文档'}
                        </Typography.Text>
                        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                          分数 {(c.score ?? 0).toFixed(4)}
                        </Typography.Text>
                        {canJump ? (
                          <Typography.Link style={{ fontSize: 12 }}>查看分块</Typography.Link>
                        ) : null}
                      </Space>
                      <Typography.Text type="secondary" style={{ fontSize: 13 }}>
                        {c.content_summary || '（无摘要）'}
                      </Typography.Text>
                    </Space>
                  </List.Item>
                )
              }}
            />
          ),
        },
      ]}
    />
  )
}

export function ChatPage() {
  const { token, apiKey } = useAuth()
  const [models, setModels] = useState<ModelItem[]>([])
  const [model, setModel] = useState<string>()
  const [temperature, setTemperature] = useState(0.7)
  const [maxTokens, setMaxTokens] = useState(1024)
  const [kbIds, setKbIds] = useState<string[]>([])
  const [missStrategy, setMissStrategy] = useState<MissStrategy>('fallback')
  const [kbList, setKbList] = useState<KnowledgeBase[]>([])
  const [sessions, setSessions] = useState<UiSession[]>([])
  const [activeId, setActiveId] = useState<string>()
  const [input, setInput] = useState('')
  const [streaming, setStreaming] = useState(false)
  const [loadingSessions, setLoadingSessions] = useState(false)
  const [loadingDetail, setLoadingDetail] = useState(false)
  const [savingSettings, setSavingSettings] = useState(false)
  const abortRef = useRef<AbortController | null>(null)
  const bottomRef = useRef<HTMLDivElement | null>(null)
  const activeIdRef = useRef<string | undefined>(undefined)
  const loadedIdsRef = useRef<Set<string>>(new Set())
  const settingsReadyRef = useRef(false)

  const active = useMemo(
    () => sessions.find((s) => s.id === activeId) || sessions[0],
    [sessions, activeId],
  )

  const readyKbOptions = useMemo(
    () =>
      kbList
        .filter((kb) => kb.status === 'active')
        .map((kb) => ({
          value: kb.id,
          label: `${kb.name}${kb.doc_count > 0 ? `（${kb.doc_count} 文档）` : ''}`,
        })),
    [kbList],
  )

  useEffect(() => {
    activeIdRef.current = active?.id
  }, [active?.id])

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [active?.messages, streaming])

  const applySessionSettings = useCallback((detail: ChatSessionDetail) => {
    settingsReadyRef.current = false
    if (detail.model) setModel(detail.model)
    if (detail.temperature != null) setTemperature(detail.temperature)
    if (detail.max_tokens != null) setMaxTokens(detail.max_tokens)
    setKbIds(detail.kb_ids || [])
    setMissStrategy(detail.miss_strategy || 'fallback')
    // 下一拍再允许自动保存，避免回显触发 PATCH
    queueMicrotask(() => {
      settingsReadyRef.current = true
    })
  }, [])

  const loadDetail = useCallback(
    async (sessionId: string) => {
      if (!token || loadedIdsRef.current.has(sessionId)) return
      setLoadingDetail(true)
      try {
        const detail = await getChatSession(token, sessionId)
        loadedIdsRef.current.add(sessionId)
        setSessions((prev) =>
          prev.map((s) => (s.id === sessionId ? toUiSession(detail) : s)),
        )
        applySessionSettings(detail)
      } catch (err) {
        message.error(err instanceof ApiError ? err.message : '加载会话失败')
      } finally {
        setLoadingDetail(false)
      }
    },
    [token, applySessionSettings],
  )

  useEffect(() => {
    if (!token) {
      setSessions([])
      setActiveId(undefined)
      loadedIdsRef.current.clear()
      return
    }

    let cancelled = false
    const bootstrap = async () => {
      setLoadingSessions(true)
      try {
        let list = await listChatSessions(token)

        const migrated = localStorage.getItem(LOCAL_MIGRATED_KEY) === '1'
        if (!migrated && list.length === 0) {
          const local = readLocalSessions().filter((s) => s.messages.length > 0)
          for (const item of local) {
            const created = await createChatSession(token, {
              title: item.title || '新对话',
            })
            await replaceChatMessages(token, created.id, {
              messages: item.messages,
              title: item.title || '新对话',
            })
          }
          if (local.length) {
            list = await listChatSessions(token)
          }
          localStorage.setItem(LOCAL_MIGRATED_KEY, '1')
          localStorage.removeItem(LOCAL_SESSIONS_KEY)
        }

        if (cancelled) return

        if (!list.length) {
          const created = await createChatSession(token, { title: '新对话' })
          if (cancelled) return
          loadedIdsRef.current.add(created.id)
          setSessions([toUiSession(created)])
          setActiveId(created.id)
          applySessionSettings(created)
          return
        }

        const ui = list.map(summaryToUi)
        for (const s of ui) {
          if (s.loaded) loadedIdsRef.current.add(s.id)
        }
        setSessions(ui)
        setActiveId(ui[0].id)
      } catch (err) {
        if (!cancelled) {
          message.error(err instanceof ApiError ? err.message : '加载会话列表失败')
        }
      } finally {
        if (!cancelled) setLoadingSessions(false)
      }
    }

    void bootstrap()
    return () => {
      cancelled = true
    }
  }, [token, applySessionSettings])

  const sessionsRef = useRef(sessions)
  sessionsRef.current = sessions

  useEffect(() => {
    if (!activeId || !token) return
    if (loadedIdsRef.current.has(activeId)) {
      const current = sessionsRef.current.find((s) => s.id === activeId)
      if (!current) return
      settingsReadyRef.current = false
      if (current.model) setModel(current.model)
      if (current.temperature != null) setTemperature(current.temperature)
      if (current.max_tokens != null) setMaxTokens(current.max_tokens)
      setKbIds(current.kb_ids || [])
      setMissStrategy(current.miss_strategy || 'fallback')
      queueMicrotask(() => {
        settingsReadyRef.current = true
      })
      return
    }
    void loadDetail(activeId)
  }, [activeId, token, loadDetail])

  useEffect(() => {
    if (!apiKey) {
      setModels([])
      setModel(undefined)
      return
    }
    listModels(apiKey)
      .then((list) => {
        setModels(list)
        setModel((prev) => prev ?? list[0]?.id)
      })
      .catch((err) => {
        message.error(err instanceof ApiError ? err.message : '加载模型失败')
      })
  }, [apiKey])

  useEffect(() => {
    if (!token) {
      setKbList([])
      return
    }
    listKnowledgeBases(token)
      .then(setKbList)
      .catch(() => {
        /* 知识库列表失败不阻断普通对话 */
      })
  }, [token])

  const persistSessionSettings = useCallback(
    async (patch: {
      kb_ids?: string[]
      miss_strategy?: MissStrategy
      model?: string | null
      temperature?: number
      max_tokens?: number
    }) => {
      const sessionId = activeIdRef.current
      if (!token || !sessionId || !settingsReadyRef.current) return
      setSavingSettings(true)
      try {
        const detail = await updateChatSession(token, sessionId, patch)
        setSessions((prev) =>
          prev.map((s) =>
            s.id === sessionId
              ? {
                  ...s,
                  kb_ids: detail.kb_ids || [],
                  miss_strategy: detail.miss_strategy || 'fallback',
                  model: detail.model,
                  temperature: detail.temperature,
                  max_tokens: detail.max_tokens,
                  updatedAt: detail.updated_at,
                }
              : s,
          ),
        )
      } catch (err) {
        message.error(err instanceof ApiError ? err.message : '保存会话设置失败')
      } finally {
        setSavingSettings(false)
      }
    },
    [token],
  )

  const onKbIdsChange = (next: string[]) => {
    setKbIds(next)
    void persistSessionSettings({ kb_ids: next })
  }

  const onMissStrategyChange = (strict: boolean) => {
    const next: MissStrategy = strict ? 'strict' : 'fallback'
    setMissStrategy(next)
    void persistSessionSettings({ miss_strategy: next })
  }

  const updateActiveLocal = (updater: (s: UiSession) => UiSession) => {
    setSessions((prev) =>
      prev.map((s) =>
        s.id === activeIdRef.current
          ? updater({ ...s, updatedAt: new Date().toISOString() })
          : s,
      ),
    )
  }

  const persistMessages = async (
    sessionId: string,
    messages: ChatMessage[],
    title?: string,
  ) => {
    if (!token) return
    try {
      const detail = await replaceChatMessages(token, sessionId, {
        messages: toPersistMessages(messages),
        title,
        model: model ?? null,
        temperature,
        max_tokens: maxTokens,
      })
      loadedIdsRef.current.add(sessionId)
      setSessions((prev) =>
        prev.map((s) => (s.id === sessionId ? toUiSession(detail) : s)),
      )
    } catch (err) {
      message.error(err instanceof ApiError ? err.message : '保存对话失败')
    }
  }

  const stop = () => {
    abortRef.current?.abort()
    abortRef.current = null
    setStreaming(false)
  }

  const runCompletion = async (
    sessionId: string,
    history: ChatMessage[],
    title?: string,
  ) => {
    if (!apiKey || !model || !token) return
    setStreaming(true)
    const controller = new AbortController()
    abortRef.current = controller

    updateActiveLocal((s) => ({
      ...s,
      title: title ?? s.title,
      messages: [...history, { role: 'assistant', content: '' }],
      loaded: true,
    }))

    let assistant = ''
    let citations: Citation[] | null = null
    let isStrictMiss = false
    const useRag = kbIds.length > 0

    try {
      const handlers = {
        onDelta: (text: string) => {
          assistant += text
          const snapshot = assistant
          updateActiveLocal((s) => {
            const next = [...s.messages]
            const last = next[next.length - 1]
            if (last?.role === 'assistant') {
              next[next.length - 1] = {
                role: 'assistant',
                content: snapshot,
                citations,
                isStrictMiss,
              }
            }
            return { ...s, messages: next }
          })
        },
        onDone: () => setStreaming(false),
        onError: (err: Error) => {
          message.error(err.message)
          setStreaming(false)
        },
        onCitations: (
          cites: Citation[],
          rag: { hit: boolean; miss_strategy: string; call_model: boolean },
        ) => {
          citations = cites.length ? cites : null
          isStrictMiss =
            !rag.hit && (rag.miss_strategy === 'strict' || !rag.call_model)
          updateActiveLocal((s) => {
            const next = [...s.messages]
            const last = next[next.length - 1]
            if (last?.role === 'assistant') {
              next[next.length - 1] = {
                ...last,
                citations,
                isStrictMiss,
              }
            }
            return { ...s, messages: next }
          })
        },
      }

      if (useRag) {
        await streamKbCompletions(
          token,
          apiKey,
          {
            model,
            messages: history,
            session_id: sessionId,
            temperature,
            max_tokens: maxTokens,
            kb_ids: kbIds,
            miss_strategy: missStrategy,
          },
          handlers,
          controller.signal,
        )
      } else {
        await streamChatCompletions(
          apiKey,
          {
            model,
            messages: history,
            temperature,
            max_tokens: maxTokens,
          },
          handlers,
          controller.signal,
        )
      }
    } catch (err) {
      if ((err as Error).name !== 'AbortError') {
        message.error(err instanceof ApiError ? err.message : '调用失败')
      }
      setStreaming(false)
    } finally {
      abortRef.current = null
      if (!isStrictMiss && assistant === '知识库中未找到相关内容') {
        isStrictMiss = true
      }
      const finalMessages: ChatMessage[] = assistant
        ? [
            ...history,
            {
              role: 'assistant',
              content: assistant,
              citations,
              isStrictMiss,
            },
          ]
        : history
      updateActiveLocal((s) => ({ ...s, messages: finalMessages, loaded: true }))
      await persistMessages(sessionId, finalMessages, title)
    }
  }

  const send = async () => {
    const content = input.trim()
    if (!content || streaming || !active) return
    if (!token) {
      message.warning('请先登录')
      return
    }
    if (!apiKey) {
      message.warning('请先在 API Key 页配置 Chat Key')
      return
    }
    if (!model) {
      message.warning('请选择模型')
      return
    }

    const userMsg: ChatMessage = { role: 'user', content }
    const nextMessages = [...active.messages, userMsg]
    const nextTitle =
      active.messages.length === 0 ? content.slice(0, 24) : active.title
    updateActiveLocal((s) => ({
      ...s,
      title: nextTitle,
      messages: nextMessages,
      loaded: true,
    }))
    setInput('')
    await runCompletion(active.id, nextMessages, nextTitle)
  }

  const regenerate = async () => {
    if (!active || streaming || !token) return
    const msgs = [...active.messages]
    while (msgs.length && msgs[msgs.length - 1].role === 'assistant') {
      msgs.pop()
    }
    if (!msgs.length || msgs[msgs.length - 1].role !== 'user') {
      message.info('没有可重新生成的内容')
      return
    }
    updateActiveLocal((s) => ({ ...s, messages: msgs }))
    await runCompletion(active.id, msgs)
  }

  const createNew = async () => {
    if (!token || streaming) return
    try {
      const created = await createChatSession(token, {
        title: '新对话',
        model: model ?? null,
        temperature,
        max_tokens: maxTokens,
        kb_ids: kbIds,
        miss_strategy: missStrategy,
      })
      loadedIdsRef.current.add(created.id)
      setSessions((prev) => [toUiSession(created), ...prev])
      setActiveId(created.id)
      applySessionSettings(created)
    } catch (err) {
      message.error(err instanceof ApiError ? err.message : '创建会话失败')
    }
  }

  const removeSession = async (sessionId: string) => {
    if (!token || streaming) return
    try {
      await deleteChatSession(token, sessionId)
      loadedIdsRef.current.delete(sessionId)
      const remaining = sessions.filter((s) => s.id !== sessionId)
      if (!remaining.length) {
        setSessions([])
        setActiveId(undefined)
        await createNew()
        return
      }
      setSessions(remaining)
      if (activeId === sessionId) setActiveId(remaining[0].id)
    } catch (err) {
      message.error(err instanceof ApiError ? err.message : '删除会话失败')
    }
  }

  if (!token) {
    return (
      <Alert
        type="warning"
        showIcon
        message={
          <span>
            请先 <Link to="/login">登录</Link> 后使用 Chat；对话将保存到服务端并支持多轮上下文。
          </span>
        }
      />
    )
  }

  return (
    <div className="chat-page">
      {!apiKey && (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 16 }}
          message={
            <span>
              尚未配置 Chat 用 API Key，请前往 <Link to="/keys">API Key</Link> 创建并保存。
            </span>
          }
        />
      )}
      <div className="chat-layout">
        <Card className="chat-sidebar" title="会话" size="small">
          <Button
            block
            icon={<PlusOutlined />}
            style={{ marginBottom: 12 }}
            onClick={() => void createNew()}
            disabled={streaming || loadingSessions}
          >
            新对话
          </Button>
          {loadingSessions ? (
            <div style={{ textAlign: 'center', padding: 24 }}>
              <Spin />
            </div>
          ) : (
            <List
              size="small"
              dataSource={sessions}
              renderItem={(item) => (
                <List.Item
                  className={`chat-session-item${item.id === active?.id ? ' is-active' : ''}`}
                  onClick={() => setActiveId(item.id)}
                  actions={[
                    <Button
                      key="del"
                      type="link"
                      size="small"
                      danger
                      disabled={streaming}
                      onClick={(e) => {
                        e.stopPropagation()
                        void removeSession(item.id)
                      }}
                    >
                      删
                    </Button>,
                  ]}
                >
                  <Space direction="vertical" size={0} style={{ maxWidth: 140 }}>
                    <Typography.Text ellipsis>{item.title}</Typography.Text>
                    {item.kb_ids?.length ? (
                      <Typography.Text type="secondary" style={{ fontSize: 11 }}>
                        已挂载 {item.kb_ids.length} 个知识库
                      </Typography.Text>
                    ) : null}
                  </Space>
                </List.Item>
              )}
            />
          )}
        </Card>

        <Card className="chat-main">
          <Collapse
            size="small"
            defaultActiveKey={['settings']}
            style={{ marginBottom: 16 }}
            items={[
              {
                key: 'settings',
                label: (
                  <Space>
                    <span>会话设置</span>
                    {kbIds.length > 0 ? (
                      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                        知识库 · {kbIds.length} 个
                        {missStrategy === 'strict' ? ' · 严格模式' : ' · 允许自答'}
                      </Typography.Text>
                    ) : (
                      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                        普通对话
                      </Typography.Text>
                    )}
                    {savingSettings ? <Spin size="small" /> : null}
                  </Space>
                ),
                children: (
                  <Space direction="vertical" size={12} style={{ width: '100%' }}>
                    <Space wrap>
                      <Select
                        style={{ minWidth: 200 }}
                        placeholder="选择模型"
                        value={model}
                        onChange={(v) => {
                          setModel(v)
                          void persistSessionSettings({ model: v })
                        }}
                        options={models.map((m) => ({ value: m.id, label: m.id }))}
                        disabled={!apiKey || streaming}
                      />
                      <span>
                        temperature{' '}
                        <InputNumber
                          min={0}
                          max={2}
                          step={0.1}
                          value={temperature}
                          onChange={(v) => {
                            const next = Number(v ?? 0.7)
                            setTemperature(next)
                            void persistSessionSettings({ temperature: next })
                          }}
                          disabled={streaming}
                        />
                      </span>
                      <span>
                        max_tokens{' '}
                        <InputNumber
                          min={1}
                          max={128000}
                          value={maxTokens}
                          onChange={(v) => {
                            const next = Number(v ?? 1024)
                            setMaxTokens(next)
                            void persistSessionSettings({ max_tokens: next })
                          }}
                          disabled={streaming}
                        />
                      </span>
                    </Space>
                    <div>
                      <Typography.Text style={{ marginRight: 8 }}>知识库</Typography.Text>
                      <Select
                        mode="multiple"
                        allowClear
                        placeholder="选择本人可用的知识库（可多选）"
                        style={{ minWidth: 280, maxWidth: '100%', width: 420 }}
                        value={kbIds}
                        onChange={onKbIdsChange}
                        options={readyKbOptions}
                        optionFilterProp="label"
                        disabled={streaming}
                        maxTagCount="responsive"
                      />
                      <Typography.Paragraph
                        type="secondary"
                        style={{ margin: '6px 0 0', fontSize: 12 }}
                      >
                        清空选择即卸载知识库，恢复普通对话；仅展示状态为「正常」的知识库。
                      </Typography.Paragraph>
                    </div>
                    <Space align="center">
                      <Typography.Text>未命中策略</Typography.Text>
                      <Switch
                        checked={missStrategy === 'strict'}
                        checkedChildren="严格"
                        unCheckedChildren="允许自答"
                        onChange={onMissStrategyChange}
                        disabled={streaming || kbIds.length === 0}
                      />
                      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                        {missStrategy === 'strict'
                          ? '仅依据知识库回答，未命中返回固定提示'
                          : '未命中时允许模型自行回答'}
                      </Typography.Text>
                    </Space>
                    <Space>
                      <Button onClick={() => void regenerate()} disabled={streaming || !apiKey}>
                        重新生成
                      </Button>
                      {streaming && (
                        <Button danger onClick={stop}>
                          停止
                        </Button>
                      )}
                    </Space>
                  </Space>
                ),
              },
            ]}
          />

          <div className="chat-messages">
            {loadingDetail && !active?.messages.length ? (
              <div style={{ textAlign: 'center', padding: 48 }}>
                <Spin />
              </div>
            ) : !active?.messages.length ? (
              <Empty
                description={
                  kbIds.length
                    ? '已挂载知识库，开始提问将检索相关内容并展示引用来源'
                    : '开始一段对话（同会话将自动携带历史上下文）'
                }
              />
            ) : (
              active.messages.map((m, idx) => {
                const miss = isStrictMissMessage(m)
                return (
                  <div
                    key={`${idx}-${m.role}`}
                    className={`chat-bubble chat-${m.role}${miss ? ' chat-miss' : ''}`}
                  >
                    <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                      {m.role}
                      {miss ? ' · 知识库未命中' : ''}
                    </Typography.Text>
                    <div className="chat-bubble-body">
                      {m.content || (streaming ? '…' : '')}
                    </div>
                    {m.role === 'assistant' && m.citations && m.citations.length > 0 ? (
                      <CitationList citations={m.citations} />
                    ) : null}
                  </div>
                )
              })
            )}
            <div ref={bottomRef} />
          </div>

          <div className="chat-composer">
            <Input.TextArea
              variant="borderless"
              value={input}
              onChange={(e) => setInput(e.target.value)}
              autoSize={{ minRows: 2, maxRows: 6 }}
              placeholder={
                kbIds.length
                  ? '输入问题，将基于已挂载知识库检索回答；Enter 发送'
                  : '输入消息，Enter 发送，Shift+Enter 换行'
              }
              disabled={streaming}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && !e.shiftKey) {
                  e.preventDefault()
                  void send()
                }
              }}
            />
            <Button type="primary" onClick={() => void send()} loading={streaming}>
              发送
            </Button>
          </div>
        </Card>
      </div>
    </div>
  )
}
