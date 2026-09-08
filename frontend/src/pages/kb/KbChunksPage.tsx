import { useCallback, useEffect, useState, type Key } from 'react'
import {
  Alert,
  Button,
  Collapse,
  Form,
  Input,
  InputNumber,
  Modal,
  Space,
  Switch,
  Table,
  Typography,
  message,
} from 'antd'
import {
  ArrowLeftOutlined,
  DeleteOutlined,
  PlusOutlined,
  ReloadOutlined,
  SearchOutlined,
} from '@ant-design/icons'
import { useNavigate, useParams, useSearchParams } from 'react-router-dom'
import {
  batchChunks,
  createChunk,
  deleteChunk,
  getDocument,
  listChunks,
  rechunkDocument,
  updateChunk,
  type KbChunk,
  type KbDocument,
} from '../../api/kb'
import { ApiError } from '../../api/client'
import { useAuth } from '../../auth/AuthContext'
import { formatDateTime } from '../../utils/datetime'
import { CHUNK_EMBED_MAP, DOC_STATUS_MAP, StatusTag } from './kbLabels'

export function KbChunksPage() {
  const { kbId = '', docId = '' } = useParams()
  const [searchParams] = useSearchParams()
  const focusChunkId = searchParams.get('chunk')
  const { token } = useAuth()
  const navigate = useNavigate()

  const [doc, setDoc] = useState<KbDocument | null>(null)
  const [items, setItems] = useState<KbChunk[]>([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(20)
  const [q, setQ] = useState('')
  const [keyword, setKeyword] = useState('')
  const [loading, setLoading] = useState(false)
  const [selectedKeys, setSelectedKeys] = useState<Key[]>([])
  const [expandedKeys, setExpandedKeys] = useState<readonly Key[]>([])
  const [editDrafts, setEditDrafts] = useState<Record<string, string>>({})
  const [savingId, setSavingId] = useState<string | null>(null)

  const [addOpen, setAddOpen] = useState(false)
  const [rechunkOpen, setRechunkOpen] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [addForm] = Form.useForm()
  const [rechunkForm] = Form.useForm()

  const reloadDoc = useCallback(async () => {
    if (!token || !docId) return
    try {
      setDoc(await getDocument(token, docId))
    } catch (err) {
      message.error(err instanceof ApiError ? err.message : '加载文档失败')
      navigate(`/kb/${kbId}`)
    }
  }, [token, docId, kbId, navigate])

  const reloadChunks = useCallback(async () => {
    if (!token || !docId) return
    setLoading(true)
    try {
      const res = await listChunks(token, docId, {
        page,
        pageSize,
        q: keyword || undefined,
      })
      setItems(res.items)
      setTotal(res.total)
      setEditDrafts((prev) => {
        const next = { ...prev }
        for (const c of res.items) {
          if (next[c.id] === undefined) next[c.id] = c.content
        }
        return next
      })
      if (focusChunkId) {
        setExpandedKeys((keys) =>
          keys.includes(focusChunkId) ? keys : [...keys, focusChunkId],
        )
      }
    } catch (err) {
      message.error(err instanceof ApiError ? err.message : '加载分块失败')
    } finally {
      setLoading(false)
    }
  }, [token, docId, page, pageSize, keyword, focusChunkId])

  useEffect(() => {
    void reloadDoc()
  }, [reloadDoc])

  useEffect(() => {
    void reloadChunks()
  }, [reloadChunks])

  useEffect(() => {
    if (!focusChunkId || loading || !items.length) return
    const timer = window.setTimeout(() => {
      const el = document.querySelector(`[data-chunk-id="${focusChunkId}"]`)
      el?.scrollIntoView({ behavior: 'smooth', block: 'center' })
    }, 80)
    return () => window.clearTimeout(timer)
  }, [focusChunkId, loading, items])

  const onToggleEnabled = async (chunk: KbChunk, enabled: boolean) => {
    if (!token) return
    try {
      const updated = await updateChunk(token, chunk.id, { is_enabled: enabled })
      setItems((rows) => rows.map((r) => (r.id === updated.id ? updated : r)))
      message.success(enabled ? '已启用' : '已禁用')
    } catch (err) {
      message.error(err instanceof ApiError ? err.message : '更新失败')
    }
  }

  const onSaveContent = async (chunk: KbChunk) => {
    if (!token) return
    const content = editDrafts[chunk.id] ?? chunk.content
    if (!content.trim()) {
      message.error('分块内容不能为空')
      return
    }
    if (content === chunk.content) {
      message.info('内容未变更')
      return
    }
    Modal.confirm({
      title: '保存分块内容？',
      content:
        '保存后该分块将重新向量化，会产生少量 Embedding 费用，并标记为「人工编辑」。',
      okText: '确认保存',
      cancelText: '取消',
      onOk: async () => {
        setSavingId(chunk.id)
        try {
          const updated = await updateChunk(token, chunk.id, { content })
          setItems((rows) => rows.map((r) => (r.id === updated.id ? updated : r)))
          setEditDrafts((d) => ({ ...d, [chunk.id]: updated.content }))
          message.success('已保存并重新向量化')
        } catch (err) {
          message.error(err instanceof ApiError ? err.message : '保存失败')
        } finally {
          setSavingId(null)
        }
      },
    })
  }

  const onDeleteOne = (chunk: KbChunk) => {
    Modal.confirm({
      title: `删除分块 #${chunk.seq}？`,
      content: '将物理删除该分块及其向量，不可恢复。',
      okText: '确认删除',
      okType: 'danger',
      cancelText: '取消',
      onOk: async () => {
        if (!token) return
        try {
          await deleteChunk(token, chunk.id)
          message.success('已删除')
          setSelectedKeys((keys) => keys.filter((k) => k !== chunk.id))
          await Promise.all([reloadChunks(), reloadDoc()])
        } catch (err) {
          message.error(err instanceof ApiError ? err.message : '删除失败')
        }
      },
    })
  }

  const onBatch = (action: 'enable' | 'disable' | 'delete') => {
    if (!token || selectedKeys.length === 0) return
    const labels = { enable: '启用', disable: '禁用', delete: '删除' }
    Modal.confirm({
      title: `批量${labels[action]} ${selectedKeys.length} 个分块？`,
      okText: '确认',
      okType: action === 'delete' ? 'danger' : 'primary',
      cancelText: '取消',
      onOk: async () => {
        try {
          const res = await batchChunks(token, {
            chunk_ids: selectedKeys.map(String),
            action,
          })
          message.success(`已${labels[action]} ${res.affected} 个分块`)
          setSelectedKeys([])
          await reloadChunks()
        } catch (err) {
          message.error(err instanceof ApiError ? err.message : '批量操作失败')
        }
      },
    })
  }

  const onAdd = async () => {
    if (!token || !docId) return
    const values = await addForm.validateFields()
    setSubmitting(true)
    try {
      await createChunk(token, docId, {
        content: values.content,
        seq: values.seq ?? null,
      })
      message.success('已新增分块（将自动向量化）')
      setAddOpen(false)
      addForm.resetFields()
      await Promise.all([reloadChunks(), reloadDoc()])
    } catch (err) {
      message.error(err instanceof ApiError ? err.message : '新增失败')
    } finally {
      setSubmitting(false)
    }
  }

  const onRechunk = async () => {
    if (!token || !docId) return
    const values = await rechunkForm.validateFields()
    if (!values.confirm) {
      message.error('请勾选确认：将覆盖现有分块并重新计费')
      return
    }
    Modal.confirm({
      title: '二次确认：重新分块',
      content:
        '将删除该文档现有全部分块（含人工编辑），按新参数重新分块并重新向量化计费。此操作不可撤销，确认继续？',
      okText: '确认重新分块',
      okType: 'danger',
      cancelText: '取消',
      onOk: async () => {
        setSubmitting(true)
        try {
          const seps = values.separators
            ?.split(/[,，]/)
            .map((s: string) => s.trim())
            .filter(Boolean)
          await rechunkDocument(token, docId, {
            confirm: true,
            chunk_size: values.chunk_size ?? null,
            chunk_overlap: values.chunk_overlap ?? null,
            separators: seps?.length ? seps : null,
          })
          message.success('已开始重新分块，请稍后刷新查看进度')
          setRechunkOpen(false)
          await Promise.all([reloadDoc(), reloadChunks()])
          navigate(`/kb/${kbId}`)
        } catch (err) {
          message.error(err instanceof ApiError ? err.message : '重新分块失败')
        } finally {
          setSubmitting(false)
        }
      },
    })
  }

  return (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      <Space align="start" style={{ width: '100%', justifyContent: 'space-between' }}>
        <Space align="start">
          <Button
            type="text"
            icon={<ArrowLeftOutlined />}
            onClick={() => navigate(`/kb/${kbId}`)}
          />
          <div>
            <Typography.Title level={3} style={{ margin: 0 }}>
              分块工作台
            </Typography.Title>
            <Space wrap size={8} style={{ marginTop: 4 }}>
              <Typography.Text>{doc?.file_name || '…'}</Typography.Text>
              {doc ? <StatusTag map={DOC_STATUS_MAP} value={doc.status} /> : null}
              <Typography.Text type="secondary">
                分块参数：{doc?.chunk_size}/{doc?.chunk_overlap} token
              </Typography.Text>
            </Space>
          </div>
        </Space>
        <Space>
          <Button icon={<ReloadOutlined />} onClick={() => void Promise.all([reloadDoc(), reloadChunks()])}>
            刷新
          </Button>
          <Button
            onClick={() => {
              if (doc) {
                rechunkForm.setFieldsValue({
                  chunk_size: doc.chunk_size,
                  chunk_overlap: doc.chunk_overlap,
                  separators: (doc.separators || []).join(','),
                  confirm: false,
                })
              }
              setRechunkOpen(true)
            }}
          >
            重新分块
          </Button>
          <Button
            type="primary"
            icon={<PlusOutlined />}
            onClick={() => {
              addForm.resetFields()
              setAddOpen(true)
            }}
          >
            新增分块
          </Button>
        </Space>
      </Space>

      <Alert
        type="info"
        showIcon
        message="编辑分块内容后保存会触发该分块重新向量化（少量费用）；禁用的分块不参与检索。"
      />

      <Space wrap>
        <Input.Search
          allowClear
          placeholder="按关键词搜索分块内容"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          onSearch={(v) => {
            setPage(1)
            setKeyword(v.trim())
          }}
          style={{ width: 320 }}
          enterButton={<SearchOutlined />}
        />
        <Button
          disabled={selectedKeys.length === 0}
          onClick={() => onBatch('enable')}
        >
          批量启用
        </Button>
        <Button
          disabled={selectedKeys.length === 0}
          onClick={() => onBatch('disable')}
        >
          批量禁用
        </Button>
        <Button
          danger
          disabled={selectedKeys.length === 0}
          onClick={() => onBatch('delete')}
        >
          批量删除
        </Button>
        {selectedKeys.length > 0 ? (
          <Typography.Text type="secondary">已选 {selectedKeys.length} 项</Typography.Text>
        ) : null}
      </Space>

      <Table<KbChunk>
        rowKey="id"
        loading={loading}
        dataSource={items}
        rowSelection={{
          selectedRowKeys: selectedKeys,
          onChange: setSelectedKeys,
        }}
        expandable={{
          expandedRowKeys: expandedKeys,
          onExpandedRowsChange: setExpandedKeys,
          expandedRowRender: (row) => (
            <Space direction="vertical" style={{ width: '100%' }} size={8}>
              <Input.TextArea
                rows={6}
                value={editDrafts[row.id] ?? row.content}
                onChange={(e) =>
                  setEditDrafts((d) => ({ ...d, [row.id]: e.target.value }))
                }
              />
              <Space>
                <Button
                  type="primary"
                  loading={savingId === row.id}
                  onClick={() => void onSaveContent(row)}
                >
                  保存内容
                </Button>
                <Typography.Text type="secondary">
                  Token {row.token_count}
                  {row.is_manually_edited ? ' · 已人工编辑' : ''}
                  {row.updated_at ? ` · 更新于 ${formatDateTime(row.updated_at)}` : ''}
                </Typography.Text>
              </Space>
            </Space>
          ),
        }}
        pagination={{
          current: page,
          pageSize,
          total,
          showSizeChanger: true,
          onChange: (p, ps) => {
            setPage(p)
            setPageSize(ps)
          },
        }}
        columns={[
          {
            title: '序号',
            dataIndex: 'seq',
            width: 80,
          },
          {
            title: '内容摘要',
            dataIndex: 'content',
            ellipsis: true,
            render: (v: string) => (v.length > 120 ? `${v.slice(0, 120)}…` : v),
          },
          {
            title: 'Token',
            dataIndex: 'token_count',
            width: 80,
          },
          {
            title: '向量状态',
            dataIndex: 'embedding_status',
            width: 110,
            render: (v: string) => <StatusTag map={CHUNK_EMBED_MAP} value={v} />,
          },
          {
            title: '启用',
            dataIndex: 'is_enabled',
            width: 90,
            render: (v: boolean, row) => (
              <Switch
                checked={v}
                onChange={(checked) => void onToggleEnabled(row, checked)}
              />
            ),
          },
          {
            title: '人工编辑',
            dataIndex: 'is_manually_edited',
            width: 90,
            render: (v: boolean) => (v ? '是' : '否'),
          },
          {
            title: '操作',
            width: 100,
            render: (_, row) => (
              <Button
                type="link"
                size="small"
                danger
                icon={<DeleteOutlined />}
                onClick={() => onDeleteOne(row)}
              >
                删除
              </Button>
            ),
          },
        ]}
        locale={{ emptyText: '暂无分块' }}
        onRow={(row) => ({
          'data-chunk-id': row.id,
        })}
        rowClassName={(row) =>
          row.id === focusChunkId ? 'ant-table-row-selected kb-chunk-focus-row' : ''
        }
      />

      <Modal
        title="新增分块"
        open={addOpen}
        onCancel={() => setAddOpen(false)}
        onOk={() => void onAdd()}
        confirmLoading={submitting}
        okText="新增"
        cancelText="取消"
        destroyOnHidden
      >
        <Form form={addForm} layout="vertical">
          <Form.Item
            name="content"
            label="内容"
            rules={[{ required: true, message: '请输入分块内容' }]}
          >
            <Input.TextArea rows={6} maxLength={100000} />
          </Form.Item>
          <Form.Item
            name="seq"
            label="插入序号"
            extra="省略则追加到末尾；指定后该位置及之后的分块序号顺延"
          >
            <InputNumber min={0} style={{ width: '100%' }} placeholder="可选" />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title="重新分块"
        open={rechunkOpen}
        onCancel={() => setRechunkOpen(false)}
        onOk={() => void onRechunk()}
        confirmLoading={submitting}
        okText="开始重新分块"
        okButtonProps={{ danger: true }}
        cancelText="取消"
        destroyOnHidden
        width={520}
      >
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 16 }}
          message="覆盖警告"
          description="重新分块将删除该文档现有全部分块（含人工编辑过的内容），并按新参数重新向量化计费。此操作不可撤销。"
        />
        <Form form={rechunkForm} layout="vertical">
          <Form.Item name="chunk_size" label="分块大小（token）">
            <InputNumber min={100} max={2000} style={{ width: '100%' }} />
          </Form.Item>
          <Form.Item name="chunk_overlap" label="分块重叠（token）">
            <InputNumber min={0} max={200} style={{ width: '100%' }} />
          </Form.Item>
          <Form.Item
            name="separators"
            label="自定义分隔符"
            extra="逗号分隔；留空使用引擎默认"
          >
            <Input />
          </Form.Item>
          <Collapse
            ghost
            items={[
              {
                key: 'confirm',
                label: '确认须知',
                children: (
                  <Form.Item
                    name="confirm"
                    valuePropName="checked"
                    rules={[
                      {
                        validator: (_, v) =>
                          v
                            ? Promise.resolve()
                            : Promise.reject(
                                new Error('请确认将覆盖现有分块并重新计费'),
                              ),
                      },
                    ]}
                  >
                    <Switch
                      checkedChildren="已确认覆盖并重新计费"
                      unCheckedChildren="未确认"
                    />
                  </Form.Item>
                ),
              },
            ]}
            defaultActiveKey={['confirm']}
          />
        </Form>
      </Modal>
    </Space>
  )
}
