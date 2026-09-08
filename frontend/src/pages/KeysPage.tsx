import { useEffect, useState } from 'react'
import {
  Button,
  Card,
  DatePicker,
  Form,
  Input,
  InputNumber,
  Modal,
  Select,
  Space,
  Table,
  Tag,
  Typography,
  message,
  Alert,
} from 'antd'
import dayjs from 'dayjs'
import {
  createKey,
  deleteKey,
  listKeys,
  updateKey,
  type ApiKeyItem,
} from '../api/keys'
import { listOrgs, type OrgWithRole } from '../api/orgs'
import { ApiError } from '../api/client'
import { useAuth } from '../auth/AuthContext'
import { formatDateTime } from '../utils/datetime'

type KeyFormValues = {
  name: string
  billing_subject?: string | null
  allowed_models?: string[]
  rpm_limit?: number | null
  tpm_limit?: number | null
  expires_at?: dayjs.Dayjs | null
}

function formatLimits(row: ApiKeyItem): string {
  const parts: string[] = []
  if (row.rpm_limit != null) parts.push(`RPM ${row.rpm_limit}`)
  if (row.tpm_limit != null) parts.push(`TPM ${row.tpm_limit}`)
  return parts.length ? parts.join(' / ') : '不限'
}

function formatBillingSubject(v: string | null): string {
  if (!v) return '账户默认'
  if (v === 'personal') return '个人钱包'
  if (v.startsWith('org:')) return `组织 ${v.slice(4)}`
  return v
}

export function KeysPage() {
  const { token, apiKey, setChatApiKey } = useAuth()
  const [items, setItems] = useState<ApiKeyItem[]>([])
  const [orgs, setOrgs] = useState<OrgWithRole[]>([])
  const [loading, setLoading] = useState(false)
  const [creating, setCreating] = useState(false)
  const [createdPlain, setCreatedPlain] = useState<string | null>(null)
  const [createOpen, setCreateOpen] = useState(false)
  const [editRow, setEditRow] = useState<ApiKeyItem | null>(null)
  const [createForm] = Form.useForm<KeyFormValues>()
  const [editForm] = Form.useForm<KeyFormValues>()

  const billingOptions = [
    { value: '', label: '账户默认' },
    { value: 'personal', label: '个人钱包' },
    ...orgs
      .filter((o) => o.status === 0)
      .map((o) => ({
        value: `org:${o.id}`,
        label: `组织：${o.name}`,
      })),
  ]

  const reload = async () => {
    if (!token) return
    setLoading(true)
    try {
      const [keys, orgList] = await Promise.all([listKeys(token), listOrgs(token)])
      setItems(keys)
      setOrgs(orgList)
    } catch (err) {
      message.error(err instanceof ApiError ? err.message : '加载失败')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    void reload()
  }, [token])

  useEffect(() => {
    if (editRow) {
      editForm.setFieldsValue({
        name: editRow.name,
        billing_subject: editRow.billing_subject ?? '',
        allowed_models: editRow.allowed_models || [],
        rpm_limit: editRow.rpm_limit,
        tpm_limit: editRow.tpm_limit,
        expires_at: editRow.expires_at ? dayjs(editRow.expires_at) : null,
      })
    }
  }, [editRow, editForm])

  const keyFormFields = (
    <>
      <Form.Item
        label="名称"
        name="name"
        rules={[{ required: true, message: '请输入名称' }]}
      >
        <Input placeholder="default" />
      </Form.Item>
      <Form.Item
        label="默认计费主体"
        name="billing_subject"
        extra="空=账户默认；personal=个人钱包；org:uuid=指定组织钱包"
      >
        <Select
          allowClear
          options={billingOptions}
          placeholder="账户默认"
        />
      </Form.Item>
      <Form.Item
        label="允许模型"
        name="allowed_models"
        extra="留空表示继承角色授权；填写后仅能调用列表内模型（与角色授权取交集）"
      >
        <Select
          mode="tags"
          placeholder="模型 alias，如 gpt-4o"
          tokenSeparators={[',', ' ']}
          allowClear
        />
      </Form.Item>
      <Form.Item label="RPM 限额" name="rpm_limit" extra="每分钟请求数，空=不限">
        <InputNumber min={1} style={{ width: '100%' }} placeholder="不限" />
      </Form.Item>
      <Form.Item label="TPM 限额" name="tpm_limit" extra="每分钟 token 数，空=不限">
        <InputNumber min={1} style={{ width: '100%' }} placeholder="不限" />
      </Form.Item>
      <Form.Item label="过期时间" name="expires_at" extra="空=永不过期">
        <DatePicker showTime style={{ width: '100%' }} />
      </Form.Item>
    </>
  )

  return (
    <Space direction="vertical" size="large" style={{ width: '100%' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', gap: 16 }}>
        <div>
          <Typography.Title level={3} style={{ margin: 0 }}>
            API Key
          </Typography.Title>
          <Typography.Paragraph type="secondary" style={{ marginBottom: 0 }}>
            明文仅在创建时展示一次。可配置计费主体、模型范围、RPM/TPM 与过期时间。设为 Chat Key
            后会持久保存在本机（按账号隔离）。
          </Typography.Paragraph>
        </div>
        <Button type="primary" loading={creating} onClick={() => setCreateOpen(true)}>
          创建 Key
        </Button>
      </div>

      {apiKey && (
        <Alert
          type="success"
          showIcon
          message={`当前 Chat 使用 Key 前缀：${apiKey.slice(0, 10)}…`}
          action={
            <Button size="small" onClick={() => setChatApiKey(null)}>
              清除
            </Button>
          }
        />
      )}

      <Card>
        <Table
          rowKey="id"
          loading={loading}
          dataSource={items}
          pagination={false}
          columns={[
            { title: '名称', dataIndex: 'name' },
            { title: '前缀', dataIndex: 'prefix' },
            {
              title: '计费主体',
              dataIndex: 'billing_subject',
              render: (v: string | null) => formatBillingSubject(v),
            },
            {
              title: '模型范围',
              dataIndex: 'allowed_models',
              render: (v: string[] | undefined) =>
                v && v.length > 0 ? (
                  <Space size={[4, 4]} wrap>
                    {v.map((m) => (
                      <Tag key={m}>{m}</Tag>
                    ))}
                  </Space>
                ) : (
                  <Typography.Text type="secondary">继承角色</Typography.Text>
                ),
            },
            {
              title: '限额',
              render: (_, row) => formatLimits(row),
            },
            {
              title: '过期',
              dataIndex: 'expires_at',
              render: (v: string | null) =>
                v ? formatDateTime(v) : (
                  <Typography.Text type="secondary">永不过期</Typography.Text>
                ),
            },
            {
              title: '状态',
              dataIndex: 'status',
              render: (v: number) =>
                v === 0 ? <Tag color="green">启用</Tag> : <Tag>已禁用</Tag>,
            },
            {
              title: '创建时间',
              dataIndex: 'created_at',
              render: (v: string) => formatDateTime(v),
            },
            {
              title: '操作',
              render: (_, row) => (
                <Space>
                  {row.status === 0 && (
                    <Button size="small" onClick={() => setEditRow(row)}>
                      编辑
                    </Button>
                  )}
                  {row.status === 0 && (
                    <Button
                      size="small"
                      onClick={() => {
                        Modal.confirm({
                          title: '设为 Chat Key',
                          content:
                            '请粘贴该 Key 的完整明文（创建时已展示）。若已丢失请新建 Key。',
                          okText: '我已准备好粘贴',
                          onOk: () => {
                            let pasted = ''
                            Modal.confirm({
                              title: '粘贴 API Key 明文',
                              content: (
                                <Input.Password
                                  placeholder="sk-..."
                                  onChange={(e) => {
                                    pasted = e.target.value
                                  }}
                                />
                              ),
                              onOk: () => {
                                if (!pasted.startsWith('sk-')) {
                                  message.error('无效的 Key 格式')
                                  return Promise.reject()
                                }
                                setChatApiKey(pasted.trim())
                                message.success('已保存为 Chat Key，重新登录后仍可用')
                              },
                            })
                          },
                        })
                      }}
                    >
                      用于 Chat
                    </Button>
                  )}
                  {row.status === 0 && (
                    <Button
                      size="small"
                      danger
                      onClick={() => {
                        Modal.confirm({
                          title: '禁用该 Key？',
                          content: '禁用后无法再用于模型调用。',
                          onOk: async () => {
                            if (!token) return
                            await deleteKey(token, row.id)
                            if (apiKey?.startsWith(row.prefix.replace('...', ''))) {
                              setChatApiKey(null)
                            }
                            message.success('已禁用')
                            await reload()
                          },
                        })
                      }}
                    >
                      禁用
                    </Button>
                  )}
                </Space>
              ),
            },
          ]}
        />
      </Card>

      <Modal
        open={createOpen}
        title="创建 API Key"
        okText="创建"
        confirmLoading={creating}
        onCancel={() => {
          setCreateOpen(false)
          createForm.resetFields()
        }}
        onOk={async () => {
          if (!token) return
          const values = await createForm.validateFields()
          setCreating(true)
          try {
            const created = await createKey(token, {
              name: values.name.trim() || 'default',
              billing_subject: values.billing_subject || null,
              allowed_models: values.allowed_models || [],
              rpm_limit: values.rpm_limit ?? null,
              tpm_limit: values.tpm_limit ?? null,
              expires_at: values.expires_at
                ? values.expires_at.toISOString()
                : null,
            })
            setCreatedPlain(created.key)
            setCreateOpen(false)
            createForm.resetFields()
            await reload()
          } catch (err) {
            message.error(err instanceof ApiError ? err.message : '创建失败')
            throw err
          } finally {
            setCreating(false)
          }
        }}
      >
        <Form form={createForm} layout="vertical" initialValues={{ name: 'default' }}>
          {keyFormFields}
        </Form>
      </Modal>

      <Modal
        open={!!editRow}
        title="编辑 API Key"
        okText="保存"
        onCancel={() => setEditRow(null)}
        onOk={async () => {
          if (!token || !editRow) return
          const values = await editForm.validateFields()
          try {
            await updateKey(token, editRow.id, {
              name: values.name.trim(),
              billing_subject: values.billing_subject || null,
              allowed_models: values.allowed_models || [],
              rpm_limit: values.rpm_limit ?? null,
              tpm_limit: values.tpm_limit ?? null,
              expires_at: values.expires_at
                ? values.expires_at.toISOString()
                : null,
            })
            message.success('已更新')
            setEditRow(null)
            await reload()
          } catch (err) {
            message.error(err instanceof ApiError ? err.message : '更新失败')
            throw err
          }
        }}
      >
        <Form form={editForm} layout="vertical">
          {keyFormFields}
        </Form>
      </Modal>

      <Modal
        open={!!createdPlain}
        title="请立即保存 API Key"
        okText="保存并用于 Chat"
        cancelText="稍后"
        onCancel={() => setCreatedPlain(null)}
        onOk={() => {
          if (createdPlain) {
            setChatApiKey(createdPlain)
            message.success('已保存并设为 Chat Key，刷新/重新登录后仍可用')
          }
          setCreatedPlain(null)
        }}
      >
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 12 }}
          message="明文仅此一次展示。设为 Chat Key 后会保存在本机，重新登录同一账号可自动恢复。"
        />
        <Input.TextArea value={createdPlain || ''} readOnly autoSize={{ minRows: 2 }} />
      </Modal>
    </Space>
  )
}
