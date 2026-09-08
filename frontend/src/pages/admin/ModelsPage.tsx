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
} from 'antd'
import { PlusOutlined, EditOutlined, DeleteOutlined, BranchesOutlined, DollarOutlined } from '@ant-design/icons'
import dayjs from 'dayjs'
import { adminApi, type BillingRule, type Channel, type LLMModel, type ModelMapping } from '../../api/admin'
import { ApiError } from '../../api/client'
import { useAdminAuth } from '../../auth/AdminAuthContext'
import { formatDateTime } from '../../utils/datetime'
import { formatMoney, yuanToUnits } from '../../utils/money'

const STATUS_MAP: Record<number, { text: string; color: string }> = {
  0: { text: '启用', color: 'green' },
  1: { text: '禁用', color: 'default' },
}

const BILLING_STATUS_MAP: Record<number, { text: string; color: string }> = {
  0: { text: '启用', color: 'green' },
  1: { text: '已作废', color: 'default' },
}

/** 列表展示：内部最小货币单位 → 元/1M tokens */
function formatPrice(v: number) {
  return `${formatMoney(v)} 元/1M`
}

// ---- Billing Rule subpanel ----
function BillingRulePanel({
  token,
  model,
  onClose,
}: {
  token: string
  model: LLMModel | null
  onClose: () => void
}) {
  const [rules, setRules] = useState<BillingRule[]>([])
  const [loading, setLoading] = useState(false)
  const [addOpen, setAddOpen] = useState(false)
  const [form] = Form.useForm()

  const load = async () => {
    if (!model) return
    setLoading(true)
    try {
      setRules(await adminApi.listBillingRules(token, model.id))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { void load() }, [model?.id])

  const handleAdd = async () => {
    const values = await form.validateFields()
    // 表单按「元/1M」录入，提交前换算为最小货币单位（1 元 = 10000）
    // InputNumber 清空时为 null，不能原样 JSON 提交（后端 int 字段会 422）
    const body = {
      price_input: yuanToUnits(values.price_input as number),
      price_output: yuanToUnits(values.price_output as number),
      price_cached_input: yuanToUnits(values.price_cached_input ?? 0),
      price_per_call: yuanToUnits(values.price_per_call ?? 0),
      cost_input: yuanToUnits(values.cost_input ?? 0),
      cost_output: yuanToUnits(values.cost_output ?? 0),
      multiplier: values.multiplier || '1.000',
      currency: values.currency || 'CNY',
      effective_at: (values.effective_at as dayjs.Dayjs).toISOString(),
    }
    try {
      await adminApi.createBillingRule(token, model!.id, body)
      message.success('计费规则已创建')
      setAddOpen(false)
      form.resetFields()
      void load()
    } catch (e) {
      message.error(e instanceof ApiError ? e.message : '操作失败')
    }
  }

  const handleDelete = (rule: BillingRule) => {
    Modal.confirm({
      title: `作废计费规则 V${rule.version}？`,
      content: '将软删除该版本（不再被新请求命中），记录保留供对账；历史账单不受影响。若需恢复定价，请新建更高版本。',
      okText: '作废',
      okButtonProps: { danger: true },
      onOk: async () => {
        try {
          await adminApi.deleteBillingRule(token, model!.id, rule.id)
          message.success('已作废')
          void load()
        } catch (e) {
          message.error(e instanceof ApiError ? e.message : '作废失败')
        }
      },
    })
  }

  if (!model) return null

  return (
    <Modal
      open={!!model}
      title={`计费规则 — ${model.alias}`}
      width={880}
      onCancel={onClose}
      footer={null}
    >
      <div style={{ display: 'flex', justifyContent: 'flex-end', marginBottom: 12 }}>
        <Button type="primary" icon={<PlusOutlined />} onClick={() => setAddOpen(true)}>新建规则版本</Button>
      </div>
      <Table
        rowKey="id"
        loading={loading}
        dataSource={rules}
        pagination={false}
        size="small"
        columns={[
          { title: 'V', dataIndex: 'version', width: 40 },
          { title: '输入价格', dataIndex: 'price_input', render: formatPrice },
          { title: '输出价格', dataIndex: 'price_output', render: formatPrice },
          { title: '倍率', dataIndex: 'multiplier', width: 70 },
          { title: '货币', dataIndex: 'currency', width: 60 },
          {
            title: '生效时间',
            dataIndex: 'effective_at',
            width: 160,
            render: (v: string) => formatDateTime(v),
          },
          {
            title: '状态', dataIndex: 'status', width: 80,
            render: (v: number) => (
              <Tag color={BILLING_STATUS_MAP[v]?.color}>{BILLING_STATUS_MAP[v]?.text ?? v}</Tag>
            ),
          },
          {
            title: '操作',
            width: 80,
            render: (_: unknown, row: BillingRule) =>
              row.status === 0 ? (
                <Button
                  size="small"
                  danger
                  icon={<DeleteOutlined />}
                  onClick={() => handleDelete(row)}
                >
                  作废
                </Button>
              ) : null,
          },
        ]}
      />
      <Modal
        open={addOpen}
        title="新建计费规则版本"
        onOk={handleAdd}
        onCancel={() => { setAddOpen(false); form.resetFields() }}
        destroyOnClose
        width={560}
      >
        <Typography.Text type="secondary" style={{ display: 'block', marginBottom: 12 }}>
          价格单位：元 / 每 1M tokens（例如 0.5 表示 0.5 元/百万 token）。
        </Typography.Text>
        <Form
          form={form}
          layout="vertical"
          initialValues={{
            price_cached_input: 0,
            cost_input: 0,
            cost_output: 0,
            multiplier: '1.000',
            currency: 'CNY',
          }}
        >
          <Space style={{ width: '100%' }} size={12}>
            <Form.Item name="price_input" label="输入价格" rules={[{ required: true }]} style={{ flex: 1 }}>
              <InputNumber min={0} step={0.0001} precision={4} style={{ width: '100%' }} placeholder="0.5" addonAfter="元/1M" />
            </Form.Item>
            <Form.Item name="price_output" label="输出价格" rules={[{ required: true }]} style={{ flex: 1 }}>
              <InputNumber min={0} step={0.0001} precision={4} style={{ width: '100%' }} placeholder="向量模型可填 0" addonAfter="元/1M" />
            </Form.Item>
            <Form.Item name="price_cached_input" label="缓存输入" style={{ flex: 1 }}>
              <InputNumber min={0} step={0.0001} precision={4} style={{ width: '100%' }} addonAfter="元/1M" />
            </Form.Item>
          </Space>
          <Space style={{ width: '100%' }} size={12}>
            <Form.Item name="cost_input" label="成本输入" style={{ flex: 1 }}>
              <InputNumber min={0} step={0.0001} precision={4} style={{ width: '100%' }} addonAfter="元/1M" />
            </Form.Item>
            <Form.Item name="cost_output" label="成本输出" style={{ flex: 1 }}>
              <InputNumber min={0} step={0.0001} precision={4} style={{ width: '100%' }} addonAfter="元/1M" />
            </Form.Item>
            <Form.Item name="multiplier" label="倍率" style={{ flex: 1 }}>
              <Input />
            </Form.Item>
          </Space>
          <Space style={{ width: '100%' }} size={12}>
            <Form.Item name="currency" label="货币" style={{ flex: 1 }}>
              <Select options={[{ value: 'CNY', label: 'CNY' }, { value: 'USD', label: 'USD' }]} />
            </Form.Item>
            <Form.Item name="effective_at" label="生效时间" rules={[{ required: true }]} style={{ flex: 2 }}>
              <DatePicker showTime style={{ width: '100%' }} />
            </Form.Item>
          </Space>
        </Form>
      </Modal>
    </Modal>
  )
}

// ---- Mapping subpanel ----
function MappingPanel({
  token,
  model,
  channels,
  onClose,
}: {
  token: string
  model: LLMModel | null
  channels: Channel[]
  onClose: () => void
}) {
  const [mappings, setMappings] = useState<ModelMapping[]>([])
  const [loading, setLoading] = useState(false)
  const [addOpen, setAddOpen] = useState(false)
  const [form] = Form.useForm()

  const load = async () => {
    if (!model) return
    setLoading(true)
    try {
      setMappings(await adminApi.listModelMappings(token, model.id))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { void load() }, [model?.id])

  const handleAdd = async () => {
    const values = await form.validateFields()
    try {
      await adminApi.createModelMapping(token, model!.id, values)
      message.success('映射已添加')
      setAddOpen(false)
      form.resetFields()
      void load()
    } catch (e) {
      message.error(e instanceof ApiError ? e.message : '操作失败')
    }
  }

  const handleDelete = (m: ModelMapping) => {
    Modal.confirm({
      title: '删除该映射？',
      onOk: async () => {
        await adminApi.deleteModelMapping(token, model!.id, m.id)
        void load()
      },
    })
  }

  const channelName = (id: string) => channels.find(c => c.id === id)?.name ?? id.slice(0, 8)

  if (!model) return null

  return (
    <Modal
      open={!!model}
      title={`渠道映射 — ${model.alias}`}
      width={680}
      onCancel={onClose}
      footer={null}
    >
      <div style={{ display: 'flex', justifyContent: 'flex-end', marginBottom: 12 }}>
        <Button type="primary" icon={<PlusOutlined />} onClick={() => setAddOpen(true)}>添加映射</Button>
      </div>
      <Table
        rowKey="id"
        loading={loading}
        dataSource={mappings}
        pagination={false}
        size="small"
        columns={[
          { title: '渠道', dataIndex: 'channel_id', render: channelName },
          { title: '上游模型名', dataIndex: 'upstream_model_name' },
          { title: '权重', dataIndex: 'weight', width: 70 },
          {
            title: '状态', dataIndex: 'status', width: 80,
            render: (v: number) => <Tag color={STATUS_MAP[v]?.color}>{STATUS_MAP[v]?.text ?? v}</Tag>,
          },
          {
            title: '操作', width: 80,
            render: (_, m) => (
              <Button size="small" danger icon={<DeleteOutlined />} onClick={() => handleDelete(m)} />
            ),
          },
        ]}
      />
      <Modal
        open={addOpen}
        title="添加渠道映射"
        onOk={handleAdd}
        onCancel={() => { setAddOpen(false); form.resetFields() }}
        destroyOnClose
      >
        <Form form={form} layout="vertical" style={{ marginTop: 8 }}>
          <Form.Item name="channel_id" label="渠道" rules={[{ required: true }]}>
            <Select
              options={channels.map(c => ({ value: c.id, label: c.name }))}
              placeholder="选择渠道"
            />
          </Form.Item>
          <Form.Item name="upstream_model_name" label="上游模型名" rules={[{ required: true }]}>
            <Input placeholder="如 gpt-4o-2024-11-20" />
          </Form.Item>
          <Form.Item name="weight" label="权重" initialValue={1}>
            <InputNumber min={1} style={{ width: '100%' }} />
          </Form.Item>
          <Form.Item name="status" label="状态" initialValue={0}>
            <Select options={[{ value: 0, label: '启用' }, { value: 1, label: '禁用' }]} />
          </Form.Item>
        </Form>
      </Modal>
    </Modal>
  )
}

// ---- Main Model Table ----
export function ModelsPage() {
  const { token } = useAdminAuth()
  const [models, setModels] = useState<LLMModel[]>([])
  const [channels, setChannels] = useState<Channel[]>([])
  const [loading, setLoading] = useState(false)
  const [modalOpen, setModalOpen] = useState(false)
  const [editing, setEditing] = useState<LLMModel | null>(null)
  const [mappingModel, setMappingModel] = useState<LLMModel | null>(null)
  const [billingModel, setBillingModel] = useState<LLMModel | null>(null)
  const [form] = Form.useForm()

  if (!token) return null

  const load = async () => {
    setLoading(true)
    try {
      const [m, c] = await Promise.all([adminApi.listModels(token), adminApi.listChannels(token)])
      setModels(m)
      setChannels(c)
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

  const openEdit = (m: LLMModel) => {
    setEditing(m)
    form.setFieldsValue(m)
    setModalOpen(true)
  }

  const handleOk = async () => {
    const values = await form.validateFields()
    try {
      if (editing) {
        await adminApi.updateModel(token, editing.id, values)
        message.success('更新成功')
      } else {
        await adminApi.createModel(token, values)
        message.success('创建成功')
      }
      setModalOpen(false)
      void load()
    } catch (e) {
      message.error(e instanceof ApiError ? e.message : '操作失败')
    }
  }

  const handleDisable = (m: LLMModel) => {
    Modal.confirm({
      title: `禁用模型「${m.alias}」？`,
      onOk: async () => {
        await adminApi.deleteModel(token, m.id)
        message.success('已禁用')
        void load()
      },
    })
  }

  return (
    <Space direction="vertical" size="large" style={{ width: '100%' }}>
      <div>
        <Typography.Title level={3} style={{ marginBottom: 4 }}>模型管理</Typography.Title>
        <Typography.Text type="secondary">管理模型别名、渠道映射与计费规则。</Typography.Text>
      </div>
      <Card>
        <div style={{ display: 'flex', justifyContent: 'flex-end', marginBottom: 12 }}>
          <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>新建模型</Button>
        </div>
        <Table
          rowKey="id"
          loading={loading}
          dataSource={models}
          pagination={false}
          columns={[
            { title: '别名', dataIndex: 'alias' },
            { title: '显示名', dataIndex: 'display_name' },
            { title: '类型', dataIndex: 'model_type', width: 80 },
            { title: '可见性', dataIndex: 'visibility', width: 120 },
            {
              title: '状态', dataIndex: 'status', width: 80,
              render: (v: number) => <Tag color={STATUS_MAP[v]?.color}>{STATUS_MAP[v]?.text ?? v}</Tag>,
            },
            {
              title: '操作', width: 200,
              render: (_, row) => (
                <Space>
                  <Button size="small" icon={<BranchesOutlined />} onClick={() => setMappingModel(row)}>映射</Button>
                  <Button size="small" icon={<DollarOutlined />} onClick={() => setBillingModel(row)}>计费</Button>
                  <Button size="small" icon={<EditOutlined />} onClick={() => openEdit(row)} />
                  {row.status === 0 && (
                    <Button size="small" danger onClick={() => handleDisable(row)}>禁用</Button>
                  )}
                </Space>
              ),
            },
          ]}
        />
      </Card>

      <Modal
        open={modalOpen}
        title={editing ? '编辑模型' : '新建模型'}
        onOk={handleOk}
        onCancel={() => setModalOpen(false)}
        destroyOnClose
      >
        <Form form={form} layout="vertical" style={{ marginTop: 8 }}>
          <Form.Item name="alias" label="模型别名" rules={[{ required: true }]}>
            <Input placeholder="如 gpt-4o" />
          </Form.Item>
          <Form.Item name="display_name" label="显示名" rules={[{ required: true }]}>
            <Input placeholder="如 GPT-4o" />
          </Form.Item>
          <Form.Item name="model_type" label="类型" initialValue="chat">
            <Select options={[{ value: 'chat', label: 'Chat' }, { value: 'embedding', label: 'Embedding' }]} />
          </Form.Item>
          <Form.Item name="visibility" label="可见性" initialValue="role_restricted">
            <Select options={[
              { value: 'role_restricted', label: 'role_restricted（角色限制）' },
              { value: 'public', label: 'public（公开）' },
            ]} />
          </Form.Item>
          <Form.Item name="status" label="状态" initialValue={0}>
            <Select options={[{ value: 0, label: '启用' }, { value: 1, label: '禁用' }]} />
          </Form.Item>
        </Form>
      </Modal>

      <MappingPanel
        token={token}
        model={mappingModel}
        channels={channels}
        onClose={() => setMappingModel(null)}
      />
      <BillingRulePanel
        token={token}
        model={billingModel}
        onClose={() => setBillingModel(null)}
      />
    </Space>
  )
}
