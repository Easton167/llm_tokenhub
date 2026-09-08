import { useEffect, useState } from 'react'
import {
  Alert,
  Button,
  Card,
  Form,
  InputNumber,
  Select,
  Space,
  Table,
  Tag,
  Typography,
  message,
} from 'antd'
import { Link } from 'react-router-dom'
import {
  createPayment,
  listPayments,
  mockPaymentCallback,
  mockSignPayload,
  type PaymentOrder,
} from '../api/payments'
import { listOrgs, type OrgWithRole } from '../api/orgs'
import { ApiError } from '../api/client'
import { useAuth } from '../auth/AuthContext'
import { formatDateTime } from '../utils/datetime'
import { formatMoney, yuanToUnits } from '../utils/money'

const STATUS_COLOR: Record<string, string> = {
  created: 'blue',
  paid: 'green',
  expired: 'default',
  failed: 'red',
}

export function RechargePage() {
  const { token } = useAuth()
  const [orgs, setOrgs] = useState<OrgWithRole[]>([])
  const [orders, setOrders] = useState<PaymentOrder[]>([])
  const [loading, setLoading] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [pending, setPending] = useState<PaymentOrder | null>(null)
  const [form] = Form.useForm()

  const targetType = Form.useWatch('target_type', form) || 'personal'
  const ownerOrgs = orgs.filter((o) => o.my_org_role === 'owner' && o.status === 0)

  const reload = async () => {
    if (!token) return
    setLoading(true)
    try {
      const [o, p] = await Promise.all([listOrgs(token), listPayments(token, { limit: 50 })])
      setOrgs(o)
      setOrders(p)
    } catch (err) {
      message.error(err instanceof ApiError ? err.message : '加载失败')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    void reload()
  }, [token])

  const completeMockPay = async (order: PaymentOrder) => {
    if (!order.external_order_no) {
      message.error('缺少外部单号，无法模拟回调')
      return
    }
    try {
      const sign = await mockSignPayload({
        order_no: order.order_no,
        external_order_no: order.external_order_no,
        success: true,
        amount: order.amount,
      })
      const paid = await mockPaymentCallback({
        order_no: order.order_no,
        external_order_no: order.external_order_no,
        success: true,
        amount: order.amount,
        sign,
      })
      message.success(`支付成功，已充入充值余额，订单 ${paid.order_no}`)
      setPending(null)
      await reload()
    } catch (err) {
      message.error(err instanceof ApiError ? err.message : '模拟支付失败')
    }
  }

  return (
    <Space direction="vertical" size="large" style={{ width: '100%' }}>
      <div>
        <Typography.Title level={3} style={{ marginBottom: 4 }}>
          在线充值
        </Typography.Title>
        <Typography.Paragraph type="secondary" style={{ marginBottom: 0 }}>
          支持个人钱包与组织钱包（需 Owner）。充值只增加充值余额（现金池），不会增加赠送余额。
          本地可用 Mock 沙箱一键完成支付；Stripe 需配置密钥并跳转 Checkout。金额单位：元（内部 1 元 =
          10000）。
        </Typography.Paragraph>
      </div>

      <Card title="发起充值">
        <Form
          form={form}
          layout="vertical"
          style={{ maxWidth: 480 }}
          initialValues={{
            target_type: 'personal',
            provider: 'mock',
            yuan: 1,
          }}
          onFinish={async (values) => {
            if (!token) return
            setSubmitting(true)
            try {
              const amount = yuanToUnits(Number(values.yuan))
              if (amount < 1) {
                message.error('金额过小')
                return
              }
              const res = await createPayment(token, {
                amount,
                target_type: values.target_type,
                org_id: values.target_type === 'org' ? values.org_id : undefined,
                provider: values.provider,
                return_url: `${window.location.origin}/recharge`,
              })
              message.success(`已创建订单 ${res.order.order_no}`)
              if (values.provider === 'stripe' && res.pay_url) {
                window.open(res.pay_url, '_blank')
                message.info('已打开 Stripe Checkout，支付完成后刷新订单列表')
              } else if (values.provider === 'mock') {
                setPending(res.order)
              }
              await reload()
            } catch (err) {
              message.error(err instanceof ApiError ? err.message : '创建支付单失败')
            } finally {
              setSubmitting(false)
            }
          }}
        >
          <Form.Item name="target_type" label="充值目标" rules={[{ required: true }]}>
            <Select
              options={[
                { value: 'personal', label: '个人钱包' },
                { value: 'org', label: '组织钱包（Owner）' },
              ]}
            />
          </Form.Item>
          {targetType === 'org' && (
            <Form.Item
              name="org_id"
              label="组织"
              rules={[{ required: true, message: '请选择组织' }]}
              extra={
                ownerOrgs.length === 0 ? (
                  <span>
                    暂无 Owner 组织，请先到 <Link to="/orgs">我的组织</Link> 创建
                  </span>
                ) : null
              }
            >
              <Select
                placeholder="选择组织"
                options={ownerOrgs.map((o) => ({ value: o.id, label: o.name }))}
              />
            </Form.Item>
          )}
          <Form.Item
            name="yuan"
            label="金额（元）"
            rules={[{ required: true, message: '请输入金额' }]}
          >
            <InputNumber min={0.0001} step={1} style={{ width: '100%' }} />
          </Form.Item>
          <Form.Item name="provider" label="支付渠道" rules={[{ required: true }]}>
            <Select
              options={[
                { value: 'mock', label: 'Mock 沙箱（本地）' },
                { value: 'stripe', label: 'Stripe' },
              ]}
            />
          </Form.Item>
          <Form.Item>
            <Button type="primary" htmlType="submit" loading={submitting}>
              创建支付单
            </Button>
          </Form.Item>
        </Form>

        {pending && pending.status === 'created' && pending.provider === 'mock' && (
          <Alert
            type="info"
            showIcon
            style={{ marginTop: 8 }}
            message={`待支付订单 ${pending.order_no}（${formatMoney(pending.amount)} ${pending.currency}）`}
            description="Mock 渠道不会跳转真实收银台。点击下方按钮模拟支付成功回调并入账。"
            action={
              <Button type="primary" onClick={() => void completeMockPay(pending)}>
                模拟支付成功
              </Button>
            }
          />
        )}
      </Card>

      <Card
        title="我的支付单"
        extra={
          <Button size="small" onClick={() => void reload()} loading={loading}>
            刷新
          </Button>
        }
      >
        <Table
          rowKey="id"
          loading={loading}
          dataSource={orders}
          pagination={{ pageSize: 15 }}
          columns={[
            { title: '订单号', dataIndex: 'order_no', ellipsis: true },
            {
              title: '目标',
              dataIndex: 'target_type',
              width: 90,
              render: (v: string) => (v === 'org' ? '组织' : '个人'),
            },
            {
              title: '金额',
              dataIndex: 'amount',
              width: 110,
              render: (v: number) => formatMoney(v),
            },
            { title: '渠道', dataIndex: 'provider', width: 90 },
            {
              title: '状态',
              dataIndex: 'status',
              width: 90,
              render: (v: string) => <Tag color={STATUS_COLOR[v] || 'default'}>{v}</Tag>,
            },
            {
              title: '操作',
              width: 140,
              render: (_, row) =>
                row.status === 'created' && row.provider === 'mock' ? (
                  <Button size="small" type="link" onClick={() => void completeMockPay(row)}>
                    模拟支付
                  </Button>
                ) : null,
            },
            {
              title: '创建时间',
              dataIndex: 'created_at',
              render: (v: string) => formatDateTime(v),
            },
          ]}
        />
      </Card>
    </Space>
  )
}
