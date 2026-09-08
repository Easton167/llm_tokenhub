import { useEffect, useState } from 'react'
import {
  Button,
  Card,
  Form,
  Input,
  InputNumber,
  Select,
  Space,
  Table,
  Tabs,
  Typography,
  message,
} from 'antd'
import { ReloadOutlined } from '@ant-design/icons'
import {
  adminApi,
  type AdminKbUserStats,
  type AdminKnowledgeBase,
  type KbGlobalSettings,
} from '../../api/admin'
import { ApiError } from '../../api/client'
import { useAdminAuth } from '../../auth/AdminAuthContext'
import { formatDateTime } from '../../utils/datetime'
import { KB_STATUS_MAP, StatusTag } from '../kb/kbLabels'

function formatBytes(v: number) {
  if (v >= 1024 ** 3) return `${(v / 1024 ** 3).toFixed(2)} GB`
  if (v >= 1024 ** 2) return `${(v / 1024 ** 2).toFixed(1)} MB`
  if (v >= 1024) return `${(v / 1024).toFixed(0)} KB`
  return `${v} B`
}

function KbListTab({ token }: { token: string }) {
  const [rows, setRows] = useState<AdminKnowledgeBase[]>([])
  const [loading, setLoading] = useState(false)
  const [status, setStatus] = useState<string | undefined>()
  const [ownerFilter, setOwnerFilter] = useState('')

  const load = async () => {
    setLoading(true)
    try {
      setRows(
        await adminApi.listAdminKbs(token, {
          status,
          owner_user_id: ownerFilter.trim() || undefined,
        }),
      )
    } catch (e) {
      message.error(e instanceof ApiError ? e.message : '加载失败')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { void load() }, [token, status])

  const toggle = async (row: AdminKnowledgeBase) => {
    try {
      if (row.status === 'disabled') {
        await adminApi.enableAdminKb(token, row.id)
        message.success('已启用')
      } else {
        await adminApi.disableAdminKb(token, row.id)
        message.success('已禁用')
      }
      void load()
    } catch (e) {
      message.error(e instanceof ApiError ? e.message : '操作失败')
    }
  }

  return (
    <Card>
      <Space style={{ marginBottom: 12 }} wrap>
        <Select
          allowClear
          placeholder="状态筛选"
          style={{ width: 140 }}
          value={status}
          onChange={v => setStatus(v)}
          options={[
            { value: 'active', label: '正常' },
            { value: 'disabled', label: '已禁用' },
          ]}
        />
        <Input
          placeholder="按用户 UUID 筛选"
          value={ownerFilter}
          onChange={e => setOwnerFilter(e.target.value)}
          style={{ width: 280 }}
          allowClear
        />
        <Button icon={<ReloadOutlined />} onClick={() => void load()}>刷新</Button>
      </Space>
      <Table
        rowKey="id"
        loading={loading}
        dataSource={rows}
        scroll={{ x: 1100 }}
        columns={[
          { title: '名称', dataIndex: 'name', width: 160 },
          {
            title: '所有者',
            dataIndex: 'owner_email',
            width: 200,
            render: (v: string | null, r) => v ?? r.owner_user_id.slice(0, 8),
          },
          {
            title: 'Embedding',
            dataIndex: 'embedding_model_alias',
            width: 140,
            render: (v: string | null) => v ?? '—',
          },
          { title: '文档', dataIndex: 'doc_count', width: 80 },
          { title: '分块', dataIndex: 'chunk_count', width: 80 },
          {
            title: '存储',
            dataIndex: 'storage_bytes',
            width: 100,
            render: (v: number) => formatBytes(v),
          },
          {
            title: '状态',
            dataIndex: 'status',
            width: 100,
            render: (v: string) => <StatusTag map={KB_STATUS_MAP} value={v} />,
          },
          {
            title: '创建时间',
            dataIndex: 'created_at',
            width: 170,
            render: (v: string) => formatDateTime(v),
          },
          {
            title: '操作',
            width: 100,
            fixed: 'right',
            render: (_, row) => (
              <Button
                size="small"
                danger={row.status !== 'disabled'}
                onClick={() => void toggle(row)}
              >
                {row.status === 'disabled' ? '启用' : '禁用'}
              </Button>
            ),
          },
        ]}
      />
    </Card>
  )
}

function UserStatsTab({ token }: { token: string }) {
  const [rows, setRows] = useState<AdminKbUserStats[]>([])
  const [loading, setLoading] = useState(false)

  const load = async () => {
    setLoading(true)
    try {
      setRows(await adminApi.listKbUserStats(token))
    } catch (e) {
      message.error(e instanceof ApiError ? e.message : '加载失败')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { void load() }, [token])

  return (
    <Card>
      <div style={{ display: 'flex', justifyContent: 'flex-end', marginBottom: 12 }}>
        <Button icon={<ReloadOutlined />} onClick={() => void load()}>刷新</Button>
      </div>
      <Table
        rowKey="user_id"
        loading={loading}
        dataSource={rows}
        columns={[
          {
            title: '用户',
            dataIndex: 'email',
            render: (v: string | null, r) => v ?? r.user_id.slice(0, 8),
          },
          { title: '知识库数', dataIndex: 'kb_count', width: 110 },
          { title: '文档数', dataIndex: 'doc_count', width: 100 },
          {
            title: '存储量',
            dataIndex: 'storage_bytes',
            width: 120,
            render: (v: number) => formatBytes(v),
          },
          {
            title: '向量化累计 Token',
            dataIndex: 'embed_input_tokens',
            width: 160,
            render: (v: number) => v.toLocaleString(),
          },
        ]}
      />
    </Card>
  )
}

function SettingsTab({ token }: { token: string }) {
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [effective, setEffective] = useState<KbGlobalSettings | null>(null)
  const [form] = Form.useForm()

  const load = async () => {
    setLoading(true)
    try {
      const s = await adminApi.getKbSettings(token)
      setEffective(s)
      form.setFieldsValue({
        default_embedding_model: s.default_embedding_model,
        max_file_size_bytes: s.max_file_size_bytes,
        upload_batch_limit: s.upload_batch_limit,
        embedding_batch_size: s.embedding_batch_size,
        embedding_max_concurrent: s.embedding_max_concurrent,
      })
    } catch (e) {
      message.error(e instanceof ApiError ? e.message : '加载失败')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { void load() }, [token])

  const handleSave = async () => {
    const values = await form.validateFields()
    setSaving(true)
    try {
      const s = await adminApi.updateKbSettings(token, values)
      setEffective(s)
      message.success('全局配置已更新')
    } catch (e) {
      message.error(e instanceof ApiError ? e.message : '保存失败')
    } finally {
      setSaving(false)
    }
  }

  return (
    <Card loading={loading}>
      {effective && (
        <Typography.Paragraph type="secondary">
          当前生效：embedding=
          {effective.effective_default_embedding_model || '（未配置）'}
          ；单文件 {formatBytes(effective.effective_max_file_size_bytes)}
          ；批量 {effective.effective_upload_batch_limit}
          ；批大小 {effective.effective_embedding_batch_size}
          ；并发 {effective.effective_embedding_max_concurrent}
          。留空字段回退环境变量默认值。
        </Typography.Paragraph>
      )}
      <Form form={form} layout="vertical" style={{ maxWidth: 480 }}>
        <Form.Item name="default_embedding_model" label="默认 Embedding 模型 alias">
          <Input placeholder="须为 embedding 类型且已配置计费" allowClear />
        </Form.Item>
        <Form.Item name="max_file_size_bytes" label="单文件大小上限（字节）">
          <InputNumber min={1} style={{ width: '100%' }} placeholder="系统默认" />
        </Form.Item>
        <Form.Item name="upload_batch_limit" label="批量上传数量上限">
          <InputNumber min={1} style={{ width: '100%' }} placeholder="系统默认" />
        </Form.Item>
        <Form.Item name="embedding_batch_size" label="向量化批大小">
          <InputNumber min={1} style={{ width: '100%' }} placeholder="系统默认" />
        </Form.Item>
        <Form.Item name="embedding_max_concurrent" label="向量化并发上限">
          <InputNumber min={1} style={{ width: '100%' }} placeholder="系统默认" />
        </Form.Item>
        <Button type="primary" loading={saving} onClick={() => void handleSave()}>
          保存
        </Button>
      </Form>
    </Card>
  )
}

export function AdminKbPage() {
  const { token } = useAdminAuth()
  if (!token) return null

  return (
    <Space direction="vertical" size="large" style={{ width: '100%' }}>
      <div>
        <Typography.Title level={3} style={{ marginBottom: 4 }}>知识库管理</Typography.Title>
        <Typography.Text type="secondary">
          全局参数、用户维度统计与违规禁用（不展示文档正文与分块内容）。
        </Typography.Text>
      </div>
      <Tabs
        items={[
          { key: 'list', label: '知识库列表', children: <KbListTab token={token} /> },
          { key: 'stats', label: '用户汇总', children: <UserStatsTab token={token} /> },
          { key: 'settings', label: '全局配置', children: <SettingsTab token={token} /> },
        ]}
      />
    </Space>
  )
}
