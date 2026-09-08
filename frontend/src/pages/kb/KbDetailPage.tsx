import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Alert,
  Button,
  Card,
  Form,
  Input,
  InputNumber,
  Modal,
  Progress,
  Space,
  Table,
  Tabs,
  Typography,
  Upload,
  message,
} from 'antd'
import {
  ArrowLeftOutlined,
  CloudUploadOutlined,
  DeleteOutlined,
  ReloadOutlined,
  RedoOutlined,
} from '@ant-design/icons'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import {
  KB_DOC_PROCESSING_STATUSES,
  deleteDocument,
  formatStorageBytes,
  getKnowledgeBase,
  hitTest,
  listDocuments,
  retryDocument,
  uploadDocuments,
  validateUploadFiles,
  type HitTestResult,
  type KbDocument,
  type KnowledgeBase,
} from '../../api/kb'
import { ApiError } from '../../api/client'
import { useAuth } from '../../auth/AuthContext'
import { formatDateTime } from '../../utils/datetime'
import { formatMoney } from '../../utils/money'
import {
  DOC_STATUS_MAP,
  KB_STATUS_MAP,
  StatusTag,
  highlightQuery,
} from './kbLabels'

export function KbDetailPage() {
  const { kbId = '' } = useParams()
  const [searchParams, setSearchParams] = useSearchParams()
  const tab = searchParams.get('tab') === 'hit-test' ? 'hit-test' : 'documents'
  const { token } = useAuth()
  const navigate = useNavigate()

  const [kb, setKb] = useState<KnowledgeBase | null>(null)
  const [docs, setDocs] = useState<KbDocument[]>([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(20)
  const [loading, setLoading] = useState(false)
  const [uploading, setUploading] = useState(false)

  const [hitForm] = Form.useForm()
  const [hitLoading, setHitLoading] = useState(false)
  const [hitResult, setHitResult] = useState<HitTestResult | null>(null)

  const reloadKb = useCallback(async () => {
    if (!token || !kbId) return
    try {
      const row = await getKnowledgeBase(token, kbId)
      setKb(row)
      hitForm.setFieldsValue({
        top_k: row.top_k,
        score_threshold: row.score_threshold,
      })
    } catch (err) {
      message.error(err instanceof ApiError ? err.message : '加载知识库失败')
      navigate('/kb')
    }
  }, [token, kbId, navigate, hitForm])

  const reloadDocs = useCallback(async () => {
    if (!token || !kbId) return
    setLoading(true)
    try {
      const res = await listDocuments(token, kbId, page, pageSize)
      setDocs(res.items)
      setTotal(res.total)
    } catch (err) {
      message.error(err instanceof ApiError ? err.message : '加载文档失败')
    } finally {
      setLoading(false)
    }
  }, [token, kbId, page, pageSize])

  useEffect(() => {
    void reloadKb()
  }, [reloadKb])

  useEffect(() => {
    if (tab === 'documents') void reloadDocs()
  }, [tab, reloadDocs])

  const hasProcessing = useMemo(
    () => docs.some((d) => KB_DOC_PROCESSING_STATUSES.has(d.status)),
    [docs],
  )

  useEffect(() => {
    if (!hasProcessing || tab !== 'documents') return
    const timer = window.setInterval(() => {
      void reloadDocs()
    }, 3000)
    return () => window.clearInterval(timer)
  }, [hasProcessing, tab, reloadDocs])

  const onUpload = async (fileList: File[]) => {
    if (!token || !kbId) return
    const errMsg = validateUploadFiles(fileList)
    if (errMsg) {
      message.error(errMsg)
      return
    }
    setUploading(true)
    try {
      await uploadDocuments(token, kbId, fileList)
      message.success(`已上传 ${fileList.length} 个文件，正在异步处理`)
      setPage(1)
      await Promise.all([reloadDocs(), reloadKb()])
    } catch (err) {
      message.error(err instanceof ApiError ? err.message : '上传失败')
    } finally {
      setUploading(false)
    }
  }

  const onRetry = async (doc: KbDocument) => {
    if (!token) return
    try {
      await retryDocument(token, doc.id)
      message.success('已重新入队处理')
      await reloadDocs()
    } catch (err) {
      message.error(err instanceof ApiError ? err.message : '重试失败')
    }
  }

  const onDeleteDoc = (doc: KbDocument) => {
    Modal.confirm({
      title: `删除文档「${doc.file_name}」？`,
      content: '将删除该文档的全部分块与向量，不可恢复。',
      okText: '确认删除',
      okType: 'danger',
      cancelText: '取消',
      onOk: async () => {
        if (!token) return
        try {
          await deleteDocument(token, doc.id)
          message.success('已删除')
          await Promise.all([reloadDocs(), reloadKb()])
        } catch (err) {
          message.error(err instanceof ApiError ? err.message : '删除失败')
        }
      },
    })
  }

  const onHitTest = async () => {
    if (!token || !kbId) return
    const values = await hitForm.validateFields()
    setHitLoading(true)
    try {
      const res = await hitTest(token, kbId, {
        query: values.query,
        top_k: values.top_k,
        score_threshold: values.score_threshold,
      })
      setHitResult(res)
      if (res.hits.length === 0) {
        message.info('未命中任何分块，可尝试降低阈值或调整问题表述')
      }
    } catch (err) {
      message.error(err instanceof ApiError ? err.message : '命中测试失败')
    } finally {
      setHitLoading(false)
    }
  }

  const documentsTab = (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      {kb?.status === 'disabled' ? (
        <Alert
          type="warning"
          showIcon
          message="该知识库已被禁用"
          description="禁用后不可上传文档，也无法用于检索与问答。请联系管理员启用。"
        />
      ) : null}

      <Upload.Dragger
        multiple
        disabled={uploading || kb?.status === 'disabled'}
        showUploadList={false}
        accept=".txt,.md,.markdown,.pdf,.docx,.html,.htm,.csv,.xlsx"
        beforeUpload={(file, fileList) => {
          // 同一批选中时，仅在最后一个文件回调里触发一次上传
          if (file.uid === fileList[fileList.length - 1]?.uid) {
            void onUpload(fileList)
          }
          return Upload.LIST_IGNORE
        }}
        style={{ padding: '12px 0' }}
      >
        <p className="ant-upload-drag-icon">
          <CloudUploadOutlined />
        </p>
        <p className="ant-upload-text">点击或拖拽文件到此处批量上传</p>
        <p className="ant-upload-hint">
          支持 TXT / Markdown / PDF / Word(docx) / HTML / CSV / Excel；单文件 ≤ 20MB，单次 ≤ 10
          个
        </p>
      </Upload.Dragger>

      <Table<KbDocument>
        rowKey="id"
        loading={loading}
        dataSource={docs}
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
            title: '文件名',
            dataIndex: 'file_name',
            render: (name: string, row) => (
              <Link to={`/kb/${kbId}/documents/${row.id}`}>{name}</Link>
            ),
          },
          {
            title: '类型',
            dataIndex: 'file_type',
            width: 90,
          },
          {
            title: '大小',
            dataIndex: 'file_size_bytes',
            width: 100,
            render: (v: number) => formatStorageBytes(v),
          },
          {
            title: '状态',
            dataIndex: 'status',
            width: 120,
            render: (v: string) => <StatusTag map={DOC_STATUS_MAP} value={v} />,
          },
          {
            title: '进度',
            width: 160,
            render: (_, row) => {
              if (KB_DOC_PROCESSING_STATUSES.has(row.status)) {
                return <Progress percent={row.progress} size="small" status="active" />
              }
              if (row.status === 'ready') {
                return <Progress percent={100} size="small" />
              }
              if (row.status === 'failed' || row.status === 'insufficient_balance') {
                return <Progress percent={row.progress} size="small" status="exception" />
              }
              return <Progress percent={row.progress} size="small" />
            },
          },
          {
            title: '说明',
            dataIndex: 'error_message',
            ellipsis: true,
            render: (msg: string | null, row) => {
              if (row.status === 'insufficient_balance') {
                return (
                  <Space direction="vertical" size={0}>
                    <Typography.Text type="danger">
                      {msg || '余额不足，无法完成向量化'}
                    </Typography.Text>
                    <Link to="/recharge">前往充值</Link>
                  </Space>
                )
              }
              if (msg) {
                return (
                  <Typography.Text type="danger" ellipsis title={msg}>
                    {msg}
                  </Typography.Text>
                )
              }
              return '—'
            },
          },
          {
            title: '更新时间',
            dataIndex: 'updated_at',
            width: 170,
            render: (v: string) => formatDateTime(v),
          },
          {
            title: '操作',
            width: 200,
            render: (_, row) => (
              <Space>
                <Button
                  type="link"
                  size="small"
                  onClick={() => navigate(`/kb/${kbId}/documents/${row.id}`)}
                >
                  分块
                </Button>
                {(row.status === 'failed' ||
                  row.status === 'insufficient_balance' ||
                  row.status === 'partial') && (
                  <Button
                    type="link"
                    size="small"
                    icon={<RedoOutlined />}
                    onClick={() => void onRetry(row)}
                  >
                    重试
                  </Button>
                )}
                <Button
                  type="link"
                  size="small"
                  danger
                  icon={<DeleteOutlined />}
                  onClick={() => onDeleteDoc(row)}
                >
                  删除
                </Button>
              </Space>
            ),
          },
        ]}
        locale={{ emptyText: '暂无文档，请上传文件' }}
      />
    </Space>
  )

  const hitTestTab = (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      <Alert
        type="info"
        showIcon
        message="命中测试会对该问题做一次向量化，按 Embedding 计费（费用通常极小）"
      />
      <Card size="small">
        <Form
          form={hitForm}
          layout="vertical"
          initialValues={{ top_k: 5, score_threshold: 0.35 }}
          onFinish={() => void onHitTest()}
        >
          <Form.Item
            name="query"
            label="模拟问题"
            rules={[{ required: true, message: '请输入问题' }]}
          >
            <Input.TextArea rows={3} maxLength={4000} placeholder="输入要检索的问题…" />
          </Form.Item>
          <Space wrap>
            <Form.Item name="top_k" label="Top-K" style={{ marginBottom: 0 }}>
              <InputNumber min={1} max={50} />
            </Form.Item>
            <Form.Item
              name="score_threshold"
              label="相似度阈值"
              style={{ marginBottom: 0 }}
            >
              <InputNumber min={0} max={1} step={0.01} />
            </Form.Item>
            <Form.Item label=" " style={{ marginBottom: 0 }}>
              <Button type="primary" htmlType="submit" loading={hitLoading}>
                开始测试
              </Button>
            </Form.Item>
          </Space>
        </Form>
      </Card>

      {hitResult ? (
        <Space direction="vertical" size={12} style={{ width: '100%' }}>
          <Typography.Text type="secondary">
            命中 {hitResult.hits.length} 条 · 消耗 {hitResult.input_tokens} tokens · 预估费用 ¥
            {formatMoney(hitResult.estimated_charge_amount)}
          </Typography.Text>
          {hitResult.hits.length === 0 ? (
            <Card size="small">
              <Typography.Text type="secondary">无命中结果</Typography.Text>
            </Card>
          ) : (
            hitResult.hits.map((hit, idx) => (
              <Card
                key={hit.chunk_id}
                size="small"
                title={
                  <Space wrap>
                    <Typography.Text strong>#{idx + 1}</Typography.Text>
                    <Typography.Text type="secondary">
                      分数 {hit.score.toFixed(4)}
                    </Typography.Text>
                    <Link to={`/kb/${kbId}/documents/${hit.document_id}?chunk=${hit.chunk_id}`}>
                      {hit.document_name}
                    </Link>
                    <Typography.Text type="secondary">分块 #{hit.seq}</Typography.Text>
                  </Space>
                }
              >
                <Typography.Paragraph style={{ marginBottom: 0, whiteSpace: 'pre-wrap' }}>
                  {highlightQuery(hit.content, hitResult.query)}
                </Typography.Paragraph>
              </Card>
            ))
          )}
        </Space>
      ) : null}
    </Space>
  )

  return (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      <Space align="start" style={{ width: '100%', justifyContent: 'space-between' }}>
        <Space align="start">
          <Button
            type="text"
            icon={<ArrowLeftOutlined />}
            onClick={() => navigate('/kb')}
          />
          <div>
            <Typography.Title level={3} style={{ margin: 0 }}>
              {kb?.name || '知识库详情'}
            </Typography.Title>
            <Space wrap size={8} style={{ marginTop: 4 }}>
              {kb ? <StatusTag map={KB_STATUS_MAP} value={kb.status} /> : null}
              <Typography.Text type="secondary">
                文档 {kb?.doc_count ?? 0} · 分块 {kb?.chunk_count ?? 0} · 存储{' '}
                {formatStorageBytes(kb?.storage_bytes ?? 0)}
              </Typography.Text>
              {kb?.embedding_model ? (
                <Typography.Text type="secondary">
                  Embedding：{kb.embedding_model}
                </Typography.Text>
              ) : null}
            </Space>
            {kb?.description ? (
              <Typography.Paragraph type="secondary" style={{ marginTop: 8, marginBottom: 0 }}>
                {kb.description}
              </Typography.Paragraph>
            ) : null}
          </div>
        </Space>
        <Button icon={<ReloadOutlined />} onClick={() => void Promise.all([reloadKb(), reloadDocs()])}>
          刷新
        </Button>
      </Space>

      <Tabs
        activeKey={tab}
        onChange={(key) => {
          setSearchParams(key === 'hit-test' ? { tab: 'hit-test' } : {})
        }}
        items={[
          { key: 'documents', label: '文档列表', children: documentsTab },
          { key: 'hit-test', label: '命中测试', children: hitTestTab },
        ]}
      />
    </Space>
  )
}
