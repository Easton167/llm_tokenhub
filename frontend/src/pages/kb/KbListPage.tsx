import { useCallback, useEffect, useState } from 'react'
import {
  Button,
  Card,
  Col,
  Collapse,
  Form,
  Input,
  InputNumber,
  Modal,
  Row,
  Space,
  Statistic,
  Table,
  Typography,
  message,
} from 'antd'
import {
  DeleteOutlined,
  EditOutlined,
  PlusOutlined,
  ReloadOutlined,
} from '@ant-design/icons'
import { Link, useNavigate } from 'react-router-dom'
import {
  createKnowledgeBase,
  deleteKnowledgeBase,
  formatStorageBytes,
  listKnowledgeBases,
  updateKnowledgeBase,
  type KnowledgeBase,
  type KnowledgeBaseCreate,
} from '../../api/kb'
import { ApiError } from '../../api/client'
import { useAuth } from '../../auth/AuthContext'
import { formatDateTime } from '../../utils/datetime'
import { KB_STATUS_MAP, StatusTag } from './kbLabels'

type FormValues = {
  name: string
  description?: string
  embedding_model?: string
  chunk_size?: number
  chunk_overlap?: number
  separators?: string
  top_k?: number
  score_threshold?: number
}

function toCreatePayload(v: FormValues): KnowledgeBaseCreate {
  const seps = v.separators
    ?.split(/[,，]/)
    .map((s) => s.trim())
    .filter(Boolean)
  return {
    name: v.name.trim(),
    description: v.description?.trim() || null,
    embedding_model: v.embedding_model?.trim() || null,
    chunk_size: v.chunk_size ?? null,
    chunk_overlap: v.chunk_overlap ?? null,
    separators: seps?.length ? seps : null,
    top_k: v.top_k ?? null,
    score_threshold: v.score_threshold ?? null,
  }
}

export function KbListPage() {
  const { token } = useAuth()
  const navigate = useNavigate()
  const [items, setItems] = useState<KnowledgeBase[]>([])
  const [loading, setLoading] = useState(false)
  const [createOpen, setCreateOpen] = useState(false)
  const [editRow, setEditRow] = useState<KnowledgeBase | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const [createForm] = Form.useForm<FormValues>()
  const [editForm] = Form.useForm<FormValues>()

  const reload = useCallback(async () => {
    if (!token) return
    setLoading(true)
    try {
      setItems(await listKnowledgeBases(token))
    } catch (err) {
      message.error(err instanceof ApiError ? err.message : '加载知识库失败')
    } finally {
      setLoading(false)
    }
  }, [token])

  useEffect(() => {
    void reload()
  }, [reload])

  useEffect(() => {
    if (editRow) {
      editForm.setFieldsValue({
        name: editRow.name,
        description: editRow.description ?? undefined,
        chunk_size: editRow.chunk_size,
        chunk_overlap: editRow.chunk_overlap,
        separators: (editRow.separators || []).join(','),
        top_k: editRow.top_k,
        score_threshold: editRow.score_threshold,
      })
    }
  }, [editRow, editForm])

  const onCreate = async () => {
    if (!token) return
    const values = await createForm.validateFields()
    setSubmitting(true)
    try {
      const kb = await createKnowledgeBase(token, toCreatePayload(values))
      message.success('知识库已创建')
      setCreateOpen(false)
      createForm.resetFields()
      await reload()
      navigate(`/kb/${kb.id}`)
    } catch (err) {
      message.error(err instanceof ApiError ? err.message : '创建失败')
    } finally {
      setSubmitting(false)
    }
  }

  const onEdit = async () => {
    if (!token || !editRow) return
    const values = await editForm.validateFields()
    const seps = values.separators
      ?.split(/[,，]/)
      .map((s) => s.trim())
      .filter(Boolean)
    setSubmitting(true)
    try {
      await updateKnowledgeBase(token, editRow.id, {
        name: values.name.trim(),
        description: values.description?.trim() || null,
        chunk_size: values.chunk_size ?? null,
        chunk_overlap: values.chunk_overlap ?? null,
        separators: seps ?? [],
        top_k: values.top_k ?? null,
        score_threshold: values.score_threshold ?? null,
      })
      message.success('已保存')
      setEditRow(null)
      await reload()
    } catch (err) {
      message.error(err instanceof ApiError ? err.message : '保存失败')
    } finally {
      setSubmitting(false)
    }
  }

  const onDelete = (row: KnowledgeBase) => {
    Modal.confirm({
      title: `删除知识库「${row.name}」？`,
      content:
        '将级联删除该库下全部文档、分块与向量索引，且不可恢复。确认继续？',
      okText: '确认删除',
      okType: 'danger',
      cancelText: '取消',
      onOk: async () => {
        if (!token) return
        try {
          await deleteKnowledgeBase(token, row.id)
          message.success('已删除')
          await reload()
        } catch (err) {
          message.error(err instanceof ApiError ? err.message : '删除失败')
        }
      },
    })
  }

  const advancedFields = (
    <>
      <Form.Item
        name="chunk_size"
        label="分块大小（token）"
        extra="默认 500，范围 100–2000"
      >
        <InputNumber min={100} max={2000} style={{ width: '100%' }} />
      </Form.Item>
      <Form.Item
        name="chunk_overlap"
        label="分块重叠（token）"
        extra="默认 50，须小于分块大小"
      >
        <InputNumber min={0} max={200} style={{ width: '100%' }} />
      </Form.Item>
      <Form.Item
        name="separators"
        label="自定义分隔符"
        extra="逗号分隔；留空使用引擎默认（标题/段落/句子）"
      >
        <Input placeholder="例如：\\n\\n,\\n,。" />
      </Form.Item>
      <Form.Item name="top_k" label="默认 Top-K" extra="默认 5">
        <InputNumber min={1} max={50} style={{ width: '100%' }} />
      </Form.Item>
      <Form.Item
        name="score_threshold"
        label="相似度阈值"
        extra="默认 0.35，低于阈值的命中将被丢弃"
      >
        <InputNumber
          min={0}
          max={1}
          step={0.01}
          style={{ width: '100%' }}
        />
      </Form.Item>
    </>
  )

  return (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      <Row justify="space-between" align="middle">
        <Col>
          <Typography.Title level={3} style={{ margin: 0 }}>
            知识库
          </Typography.Title>
          <Typography.Text type="secondary">
            管理个人知识库、上传文档并调试检索效果
          </Typography.Text>
        </Col>
        <Col>
          <Space>
            <Button icon={<ReloadOutlined />} onClick={() => void reload()}>
              刷新
            </Button>
            <Button
              type="primary"
              icon={<PlusOutlined />}
              onClick={() => {
                createForm.resetFields()
                setCreateOpen(true)
              }}
            >
              新建知识库
            </Button>
          </Space>
        </Col>
      </Row>

      <Card styles={{ body: { padding: 0 } }}>
      <Table<KnowledgeBase>
        rowKey="id"
        loading={loading}
        dataSource={items}
        pagination={false}
        onRow={(row) => ({
          onClick: () => navigate(`/kb/${row.id}`),
          style: { cursor: 'pointer' },
        })}
        columns={[
          {
            title: '名称',
            dataIndex: 'name',
            render: (name: string, row) => (
              <Space direction="vertical" size={0}>
                <Link to={`/kb/${row.id}`} onClick={(e) => e.stopPropagation()}>
                  {name}
                </Link>
                {row.description ? (
                  <Typography.Text type="secondary" ellipsis style={{ maxWidth: 280 }}>
                    {row.description}
                  </Typography.Text>
                ) : null}
              </Space>
            ),
          },
          {
            title: '状态',
            dataIndex: 'status',
            width: 100,
            render: (v: string) => <StatusTag map={KB_STATUS_MAP} value={v} />,
          },
          {
            title: '文档数',
            dataIndex: 'doc_count',
            width: 90,
          },
          {
            title: '分块数',
            dataIndex: 'chunk_count',
            width: 90,
          },
          {
            title: '存储占用',
            dataIndex: 'storage_bytes',
            width: 120,
            render: (v: number) => formatStorageBytes(v),
          },
          {
            title: 'Embedding',
            dataIndex: 'embedding_model',
            width: 140,
            render: (v: string | null) => v || '—',
          },
          {
            title: '更新时间',
            dataIndex: 'updated_at',
            width: 180,
            render: (v: string) => formatDateTime(v),
          },
          {
            title: '操作',
            width: 160,
            render: (_, row) => (
              <Space onClick={(e) => e.stopPropagation()}>
                <Button
                  type="link"
                  size="small"
                  icon={<EditOutlined />}
                  onClick={() => setEditRow(row)}
                >
                  编辑
                </Button>
                <Button
                  type="link"
                  size="small"
                  danger
                  icon={<DeleteOutlined />}
                  onClick={() => onDelete(row)}
                >
                  删除
                </Button>
              </Space>
            ),
          },
        ]}
        locale={{ emptyText: '暂无知识库，点击右上角新建' }}
      />
      </Card>

      {!loading && items.length > 0 ? (
        <Row gutter={16}>
          {items.slice(0, 4).map((kb) => (
            <Col key={kb.id} xs={24} sm={12} lg={6}>
              <Card
                size="small"
                style={{ cursor: 'pointer' }}
                onClick={() => navigate(`/kb/${kb.id}`)}
                title={
                  <Space>
                    <span>{kb.name}</span>
                    <StatusTag map={KB_STATUS_MAP} value={kb.status} />
                  </Space>
                }
              >
                <Row gutter={8}>
                  <Col span={8}>
                    <Statistic title="文档" value={kb.doc_count} valueStyle={{ fontSize: 18 }} />
                  </Col>
                  <Col span={8}>
                    <Statistic title="分块" value={kb.chunk_count} valueStyle={{ fontSize: 18 }} />
                  </Col>
                  <Col span={8}>
                    <Statistic
                      title="存储"
                      value={formatStorageBytes(kb.storage_bytes)}
                      valueStyle={{ fontSize: 14 }}
                    />
                  </Col>
                </Row>
              </Card>
            </Col>
          ))}
        </Row>
      ) : null}

      <Modal
        title="新建知识库"
        open={createOpen}
        onCancel={() => setCreateOpen(false)}
        onOk={() => void onCreate()}
        confirmLoading={submitting}
        okText="创建"
        cancelText="取消"
        destroyOnHidden
        width={560}
      >
        <Form
          form={createForm}
          layout="vertical"
          initialValues={{
            chunk_size: 500,
            chunk_overlap: 50,
            top_k: 5,
            score_threshold: 0.35,
          }}
        >
          <Form.Item
            name="name"
            label="名称"
            rules={[{ required: true, message: '请输入名称' }]}
          >
            <Input maxLength={128} placeholder="例如：产品手册" />
          </Form.Item>
          <Form.Item name="description" label="描述">
            <Input.TextArea rows={2} maxLength={4000} placeholder="可选" />
          </Form.Item>
          <Form.Item
            name="embedding_model"
            label="Embedding 模型"
            extra="留空则使用系统默认；创建后不可更换"
          >
            <Input placeholder="模型 alias（可选）" />
          </Form.Item>
          <Collapse
            ghost
            items={[
              {
                key: 'advanced',
                label: '高级选项（分块与检索参数）',
                children: advancedFields,
              },
            ]}
          />
        </Form>
      </Modal>

      <Modal
        title="编辑知识库"
        open={!!editRow}
        onCancel={() => setEditRow(null)}
        onOk={() => void onEdit()}
        confirmLoading={submitting}
        okText="保存"
        cancelText="取消"
        destroyOnHidden
        width={560}
      >
        <Form form={editForm} layout="vertical">
          <Form.Item
            name="name"
            label="名称"
            rules={[{ required: true, message: '请输入名称' }]}
          >
            <Input maxLength={128} />
          </Form.Item>
          <Form.Item name="description" label="描述">
            <Input.TextArea rows={2} maxLength={4000} />
          </Form.Item>
          {editRow ? (
            <Typography.Paragraph type="secondary" style={{ marginBottom: 12 }}>
              Embedding 模型：{editRow.embedding_model || '—'}（创建后不可更换）
            </Typography.Paragraph>
          ) : null}
          <Collapse
            defaultActiveKey={['advanced']}
            ghost
            items={[
              {
                key: 'advanced',
                label: '分块与检索参数',
                children: advancedFields,
              },
            ]}
          />
        </Form>
      </Modal>
    </Space>
  )
}
