import { useEffect, useState } from 'react'
import {
  Button,
  Card,
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
  Tabs,
} from 'antd'
import { PlusOutlined, EditOutlined, DeleteOutlined, KeyOutlined } from '@ant-design/icons'
import { adminApi, type Channel, type ChannelKey, type Provider } from '../../api/admin'
import { ApiError } from '../../api/client'
import { useAdminAuth } from '../../auth/AdminAuthContext'
import { formatDateTime } from '../../utils/datetime'

const STATUS_MAP: Record<number, { text: string; color: string }> = {
  0: { text: '启用', color: 'green' },
  1: { text: '禁用', color: 'default' },
}

function ProviderTab({ token }: { token: string }) {
  const [items, setItems] = useState<Provider[]>([])
  const [loading, setLoading] = useState(false)
  const [modalOpen, setModalOpen] = useState(false)
  const [editing, setEditing] = useState<Provider | null>(null)
  const [form] = Form.useForm()

  const load = async () => {
    setLoading(true)
    try {
      setItems(await adminApi.listProviders(token))
    } catch (e) {
      message.error(e instanceof ApiError ? e.message : '加载失败')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { void load() }, [token])

  const openCreate = () => {
    setEditing(null)
    form.resetFields()
    setModalOpen(true)
  }

  const openEdit = (p: Provider) => {
    setEditing(p)
    form.setFieldsValue(p)
    setModalOpen(true)
  }

  const handleOk = async () => {
    const values = await form.validateFields()
    try {
      if (editing) {
        await adminApi.updateProvider(token, editing.id, values)
        message.success('更新成功')
      } else {
        await adminApi.createProvider(token, values)
        message.success('创建成功')
      }
      setModalOpen(false)
      void load()
    } catch (e) {
      message.error(e instanceof ApiError ? e.message : '操作失败')
    }
  }

  const handleDelete = (p: Provider) => {
    Modal.confirm({
      title: `删除 Provider「${p.name}」？`,
      content: '需先删除该 Provider 下所有渠道。',
      onOk: async () => {
        try {
          await adminApi.deleteProvider(token, p.id)
          message.success('已删除')
          void load()
        } catch (e) {
          message.error(e instanceof ApiError ? e.message : '删除失败')
        }
      },
    })
  }

  return (
    <>
      <div style={{ display: 'flex', justifyContent: 'flex-end', marginBottom: 12 }}>
        <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>新建 Provider</Button>
      </div>
      <Table
        rowKey="id"
        loading={loading}
        dataSource={items}
        pagination={false}
        columns={[
          { title: '名称', dataIndex: 'name' },
          { title: '适配器类型', dataIndex: 'adapter_type' },
          { title: '默认 Base URL', dataIndex: 'default_base_url', ellipsis: true },
          {
            title: '创建时间',
            dataIndex: 'created_at',
            width: 180,
            render: (v: string) => formatDateTime(v),
          },
          {
            title: '操作', width: 120,
            render: (_, row) => (
              <Space>
                <Button size="small" icon={<EditOutlined />} onClick={() => openEdit(row)} />
                <Button size="small" danger icon={<DeleteOutlined />} onClick={() => handleDelete(row)} />
              </Space>
            ),
          },
        ]}
      />
      <Modal
        open={modalOpen}
        title={editing ? '编辑 Provider' : '新建 Provider'}
        onOk={handleOk}
        onCancel={() => setModalOpen(false)}
        destroyOnClose
      >
        <Form form={form} layout="vertical" style={{ marginTop: 8 }}>
          <Form.Item name="name" label="名称" rules={[{ required: true }]}>
            <Input placeholder="如 OpenAI" />
          </Form.Item>
          <Form.Item name="adapter_type" label="适配器类型" rules={[{ required: true }]}>
            <Select options={[
              { value: 'openai_compatible', label: 'OpenAI Compatible' },
              { value: 'anthropic', label: 'Anthropic' },
            ]} />
          </Form.Item>
          <Form.Item name="default_base_url" label="默认 Base URL">
            <Input placeholder="https://api.openai.com/v1" />
          </Form.Item>
        </Form>
      </Modal>
    </>
  )
}

function ChannelKeyDrawer({
  token,
  channel,
  onClose,
}: {
  token: string
  channel: Channel | null
  onClose: () => void
}) {
  const [keys, setKeys] = useState<ChannelKey[]>([])
  const [loading, setLoading] = useState(false)
  const [addOpen, setAddOpen] = useState(false)
  const [form] = Form.useForm()

  const load = async () => {
    if (!channel) return
    setLoading(true)
    try {
      setKeys(await adminApi.listChannelKeys(token, channel.id))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { void load() }, [channel?.id])

  const handleAdd = async () => {
    const values = await form.validateFields()
    try {
      await adminApi.createChannelKey(token, channel!.id, values)
      message.success('密钥已添加')
      setAddOpen(false)
      form.resetFields()
      void load()
    } catch (e) {
      message.error(e instanceof ApiError ? e.message : '操作失败')
    }
  }

  const handleToggle = async (k: ChannelKey) => {
    try {
      await adminApi.updateChannelKey(token, channel!.id, k.id, { status: k.status === 0 ? 1 : 0 })
      void load()
    } catch (e) {
      message.error(e instanceof ApiError ? e.message : '操作失败')
    }
  }

  const handleDelete = (k: ChannelKey) => {
    Modal.confirm({
      title: '删除该密钥？',
      onOk: async () => {
        await adminApi.deleteChannelKey(token, channel!.id, k.id)
        void load()
      },
    })
  }

  if (!channel) return null

  return (
    <Modal
      open={!!channel}
      title={`渠道密钥 — ${channel.name}`}
      width={680}
      onCancel={onClose}
      footer={null}
    >
      <div style={{ display: 'flex', justifyContent: 'flex-end', marginBottom: 12 }}>
        <Button type="primary" icon={<PlusOutlined />} onClick={() => setAddOpen(true)}>添加密钥</Button>
      </div>
      <Table
        rowKey="id"
        loading={loading}
        dataSource={keys}
        pagination={false}
        size="small"
        columns={[
          { title: '脱敏 Key', dataIndex: 'key_masked' },
          { title: '权重', dataIndex: 'weight', width: 60 },
          {
            title: '状态', dataIndex: 'status', width: 80,
            render: (v: number) => <Tag color={STATUS_MAP[v]?.color}>{STATUS_MAP[v]?.text ?? v}</Tag>,
          },
          {
            title: '操作', width: 120,
            render: (_, k) => (
              <Space>
                <Button size="small" onClick={() => handleToggle(k)}>
                  {k.status === 0 ? '禁用' : '启用'}
                </Button>
                <Button size="small" danger icon={<DeleteOutlined />} onClick={() => handleDelete(k)} />
              </Space>
            ),
          },
        ]}
      />
      <Modal
        open={addOpen}
        title="添加渠道密钥"
        onOk={handleAdd}
        onCancel={() => { setAddOpen(false); form.resetFields() }}
        destroyOnClose
      >
        <Form form={form} layout="vertical" style={{ marginTop: 8 }}>
          <Form.Item name="api_key" label="API Key（明文）" rules={[{ required: true }]}>
            <Input.Password placeholder="sk-..." />
          </Form.Item>
          <Form.Item name="weight" label="权重" initialValue={1}>
            <InputNumber min={1} style={{ width: '100%' }} />
          </Form.Item>
          <Form.Item name="rpm_limit" label="RPM 限制（留空不限）">
            <InputNumber min={1} style={{ width: '100%' }} />
          </Form.Item>
          <Form.Item name="tpm_limit" label="TPM 限制（留空不限）">
            <InputNumber min={1} style={{ width: '100%' }} />
          </Form.Item>
        </Form>
      </Modal>
    </Modal>
  )
}

function ChannelTab({ token }: { token: string }) {
  const [providers, setProviders] = useState<Provider[]>([])
  const [items, setItems] = useState<Channel[]>([])
  const [loading, setLoading] = useState(false)
  const [modalOpen, setModalOpen] = useState(false)
  const [editing, setEditing] = useState<Channel | null>(null)
  const [keyChannel, setKeyChannel] = useState<Channel | null>(null)
  const [form] = Form.useForm()

  const load = async () => {
    setLoading(true)
    try {
      const [ch, pv] = await Promise.all([
        adminApi.listChannels(token),
        adminApi.listProviders(token),
      ])
      setItems(ch)
      setProviders(pv)
    } catch (e) {
      message.error(e instanceof ApiError ? e.message : '加载失败')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { void load() }, [token])

  const providerName = (id: string) => providers.find(p => p.id === id)?.name ?? id.slice(0, 8)

  const openCreate = () => {
    setEditing(null)
    form.resetFields()
    setModalOpen(true)
  }

  const openEdit = (c: Channel) => {
    setEditing(c)
    form.setFieldsValue(c)
    setModalOpen(true)
  }

  const handleOk = async () => {
    const values = await form.validateFields()
    try {
      if (editing) {
        await adminApi.updateChannel(token, editing.id, values)
        message.success('更新成功')
      } else {
        await adminApi.createChannel(token, values)
        message.success('创建成功')
      }
      setModalOpen(false)
      void load()
    } catch (e) {
      message.error(e instanceof ApiError ? e.message : '操作失败')
    }
  }

  const handleDelete = (c: Channel) => {
    Modal.confirm({
      title: `删除渠道「${c.name}」？`,
      content: '将同时删除该渠道下所有密钥。',
      onOk: async () => {
        try {
          await adminApi.deleteChannel(token, c.id)
          message.success('已删除')
          void load()
        } catch (e) {
          message.error(e instanceof ApiError ? e.message : '删除失败')
        }
      },
    })
  }

  return (
    <>
      <div style={{ display: 'flex', justifyContent: 'flex-end', marginBottom: 12 }}>
        <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>新建渠道</Button>
      </div>
      <Table
        rowKey="id"
        loading={loading}
        dataSource={items}
        pagination={false}
        columns={[
          { title: '名称', dataIndex: 'name' },
          { title: 'Provider', dataIndex: 'provider_id', render: providerName },
          { title: 'Base URL', dataIndex: 'base_url', ellipsis: true },
          { title: '优先级', dataIndex: 'priority', width: 80 },
          { title: '权重', dataIndex: 'weight', width: 70 },
          {
            title: '状态', dataIndex: 'status', width: 80,
            render: (v: number) => <Tag color={STATUS_MAP[v]?.color}>{STATUS_MAP[v]?.text ?? v}</Tag>,
          },
          {
            title: '操作', width: 150,
            render: (_, row) => (
              <Space>
                <Button size="small" icon={<KeyOutlined />} onClick={() => setKeyChannel(row)}>密钥</Button>
                <Button size="small" icon={<EditOutlined />} onClick={() => openEdit(row)} />
                <Button size="small" danger icon={<DeleteOutlined />} onClick={() => handleDelete(row)} />
              </Space>
            ),
          },
        ]}
      />
      <Modal
        open={modalOpen}
        title={editing ? '编辑渠道' : '新建渠道'}
        onOk={handleOk}
        onCancel={() => setModalOpen(false)}
        destroyOnClose
        width={560}
      >
        <Form form={form} layout="vertical" style={{ marginTop: 8 }}>
          <Form.Item name="provider_id" label="Provider" rules={[{ required: true }]}>
            <Select
              options={providers.map(p => ({ value: p.id, label: p.name }))}
              placeholder="选择 Provider"
            />
          </Form.Item>
          <Form.Item name="name" label="渠道名称" rules={[{ required: true }]}>
            <Input placeholder="如 openai-main" />
          </Form.Item>
          <Form.Item name="base_url" label="Base URL" rules={[{ required: true }]}>
            <Input placeholder="https://api.openai.com/v1" />
          </Form.Item>
          <Form.Item name="adapter_type" label="适配器类型" rules={[{ required: true }]} initialValue="openai_compatible">
            <Select options={[{ value: 'openai_compatible', label: 'OpenAI Compatible' }]} />
          </Form.Item>
          <Space style={{ width: '100%' }} size={16}>
            <Form.Item name="priority" label="优先级" initialValue={0} style={{ flex: 1 }}>
              <InputNumber style={{ width: '100%' }} />
            </Form.Item>
            <Form.Item name="weight" label="权重" initialValue={1} style={{ flex: 1 }}>
              <InputNumber min={1} style={{ width: '100%' }} />
            </Form.Item>
            <Form.Item name="timeout_ms" label="超时(ms)" initialValue={120000} style={{ flex: 1 }}>
              <InputNumber min={1000} style={{ width: '100%' }} />
            </Form.Item>
            <Form.Item name="max_retries" label="最大重试" initialValue={2} style={{ flex: 1 }}>
              <InputNumber min={0} style={{ width: '100%' }} />
            </Form.Item>
          </Space>
          <Form.Item name="status" label="状态" initialValue={0}>
            <Select options={[
              { value: 0, label: '启用' },
              { value: 1, label: '禁用' },
            ]} />
          </Form.Item>
        </Form>
      </Modal>
      <ChannelKeyDrawer
        token={token}
        channel={keyChannel}
        onClose={() => setKeyChannel(null)}
      />
    </>
  )
}

export function ChannelsPage() {
  const { token } = useAdminAuth()
  if (!token) return null

  return (
    <Space direction="vertical" size="large" style={{ width: '100%' }}>
      <div>
        <Typography.Title level={3} style={{ marginBottom: 4 }}>渠道管理</Typography.Title>
        <Typography.Text type="secondary">管理上游厂商（Provider）、调用渠道及渠道密钥。</Typography.Text>
      </div>
      <Card>
        <Tabs
          items={[
            { key: 'channels', label: '渠道', children: <ChannelTab token={token} /> },
            { key: 'providers', label: 'Provider', children: <ProviderTab token={token} /> },
          ]}
        />
      </Card>
    </Space>
  )
}
