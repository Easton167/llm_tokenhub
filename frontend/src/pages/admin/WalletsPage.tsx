import { useEffect, useState } from 'react'
import {
  Button,
  Card,
  DatePicker,
  Descriptions,
  Form,
  Input,
  InputNumber,
  Modal,
  Radio,
  Select,
  Space,
  Table,
  Tabs,
  Tag,
  Typography,
  message,
} from 'antd'
import { ReloadOutlined } from '@ant-design/icons'
import type { Dayjs } from 'dayjs'
import {
  adminApi,
  type AdminUser,
  type GiftGrantCreate,
  type GiftGrantOut,
  type GiftReason,
  type WalletOut,
  type WalletTransaction,
} from '../../api/admin'
import { ApiError } from '../../api/client'
import { useAdminAuth } from '../../auth/AdminAuthContext'
import { formatDateTime } from '../../utils/datetime'

function fmt(v: number) {
  return (v / 10_000).toFixed(4)
}

const TX_TYPE_MAP: Record<string, { text: string; color: string }> = {
  hold: { text: '预扣', color: 'orange' },
  capture: { text: '实扣', color: 'red' },
  release: { text: '释放', color: 'blue' },
  recharge: { text: '充值', color: 'green' },
  adjust: { text: '调账', color: 'purple' },
  gift_grant: { text: '赠送', color: 'cyan' },
  gift_revoke: { text: '撤回赠金', color: 'gold' },
  gift_expire: { text: '赠金过期', color: 'default' },
}

const GIFT_STATUS_MAP: Record<string, { text: string; color: string }> = {
  active: { text: '有效', color: 'green' },
  used_up: { text: '已用尽', color: 'default' },
  expired: { text: '已过期', color: 'orange' },
  revoked: { text: '已撤回', color: 'red' },
}

const GIFT_REASON_MAP: Record<string, { text: string; color: string }> = {
  promotion: { text: '营销活动', color: 'blue' },
  compensation: { text: '客诉补偿', color: 'orange' },
  internal: { text: '内部测试', color: 'purple' },
  partnership: { text: '商务合作', color: 'cyan' },
  other: { text: '其他', color: 'default' },
}

type GiftExpiry = '30' | '90' | 'custom' | 'never'

type GiftFormValues = {
  amount: number
  expiry: GiftExpiry
  expires_at?: Dayjs
  reason: GiftReason
  remark?: string
}

function newIdempotencyKey(): string {
  return crypto.randomUUID()
}

function buildGiftBody(values: GiftFormValues, key: string): GiftGrantCreate {
  const body: GiftGrantCreate = {
    amount: values.amount,
    reason: values.reason,
    remark: values.remark || undefined,
    idempotency_key: key,
  }
  if (values.expiry === '30') body.expires_in_days = 30
  else if (values.expiry === '90') body.expires_in_days = 90
  else if (values.expiry === 'never') body.never_expires = true
  else if (values.expiry === 'custom' && values.expires_at) {
    body.expires_at = values.expires_at.endOf('day').toISOString()
  }
  return body
}

function WalletDetailModal({
  token,
  user,
  users,
  onClose,
}: {
  token: string
  user: AdminUser | null
  users: AdminUser[]
  onClose: () => void
}) {
  const [wallet, setWallet] = useState<WalletOut | null>(null)
  const [txs, setTxs] = useState<WalletTransaction[]>([])
  const [grants, setGrants] = useState<GiftGrantOut[]>([])
  const [loading, setLoading] = useState(false)
  const [rechargeOpen, setRechargeOpen] = useState(false)
  const [adjustOpen, setAdjustOpen] = useState(false)
  const [giftOpen, setGiftOpen] = useState(false)
  const [giftKey, setGiftKey] = useState('')
  const [giftSubmitting, setGiftSubmitting] = useState(false)
  const [rechargeForm] = Form.useForm()
  const [adjustForm] = Form.useForm()
  const [giftForm] = Form.useForm<GiftFormValues>()

  const operatorLabel = (id: string) => {
    const found = users.find((u) => u.id === id)
    return found?.email ?? id.slice(0, 8)
  }

  const load = async () => {
    if (!user) return
    setLoading(true)
    try {
      const w = await adminApi.getUserWallet(token, user.id)
      const [t, g] = await Promise.all([
        adminApi.listUserTransactions(token, user.id, { limit: 50 }),
        adminApi.listWalletGifts(token, w.id, { limit: 100 }),
      ])
      setWallet(w)
      setTxs(t)
      setGrants(g)
    } catch (e) {
      message.error(e instanceof ApiError ? e.message : '加载失败')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { void load() }, [user?.id])

  const handleRecharge = async () => {
    const values = await rechargeForm.validateFields()
    try {
      await adminApi.rechargeWallet(token, user!.id, values.amount, values.remark)
      message.success('充值成功')
      setRechargeOpen(false)
      rechargeForm.resetFields()
      void load()
    } catch (e) {
      message.error(e instanceof ApiError ? e.message : '充值失败')
    }
  }

  const handleAdjust = async () => {
    const values = await adjustForm.validateFields()
    try {
      await adminApi.adjustWallet(token, user!.id, values.amount, values.remark)
      message.success('调账成功')
      setAdjustOpen(false)
      adjustForm.resetFields()
      void load()
    } catch (e) {
      message.error(e instanceof ApiError ? e.message : '调账失败')
    }
  }

  const openGift = () => {
    setGiftKey(newIdempotencyKey())
    setGiftSubmitting(false)
    giftForm.resetFields()
    setGiftOpen(true)
  }

  const closeGift = () => {
    setGiftOpen(false)
    giftForm.resetFields()
  }

  const doGrant = async (values: GiftFormValues) => {
    if (giftSubmitting) return
    setGiftSubmitting(true)
    try {
      await adminApi.grantUserGift(token, user!.id, buildGiftBody(values, giftKey))
      message.success('赠送成功')
      closeGift()
      void load()
    } catch (e) {
      message.error(e instanceof ApiError ? e.message : '赠送失败')
    } finally {
      setGiftSubmitting(false)
    }
  }

  const handleGift = async () => {
    const values = await giftForm.validateFields()
    if (values.expiry === 'never') {
      Modal.confirm({
        title: '确认为永久有效？',
        content: '永久有效的赠金不会自动过期，请确认这是有意为之。',
        okText: '确认永久有效',
        cancelText: '取消',
        onOk: () => doGrant(values),
      })
      return
    }
    await doGrant(values)
  }

  const handleRevoke = (row: GiftGrantOut) => {
    Modal.confirm({
      title: '撤回该赠送批次？',
      content: `仅撤回未消耗部分（当前剩余 ${fmt(row.remaining)}），已消费金额不追回。`,
      okText: '确认撤回',
      okButtonProps: { danger: true },
      cancelText: '取消',
      onOk: async () => {
        try {
          await adminApi.revokeGift(token, row.id)
          message.success('已撤回未消耗部分')
          void load()
        } catch (e) {
          if (e instanceof ApiError && e.code === 'gift_already_consumed') {
            message.error('该批次已消耗完，无可撤回余额')
          } else {
            message.error(e instanceof ApiError ? e.message : '撤回失败')
          }
        }
      },
    })
  }

  if (!user) return null

  const giftBalance = wallet?.gift_balance ?? 0
  const giftHeld = wallet?.gift_held ?? 0
  const totalAvailable = wallet?.total_available ?? ((wallet?.balance ?? 0) + (wallet?.credit_limit ?? 0))

  return (
    <Modal
      open={!!user}
      title={`钱包 — ${user.nickname ?? user.email}`}
      onCancel={onClose}
      footer={null}
      width={900}
    >
      {wallet && (
        <Descriptions size="small" bordered style={{ marginBottom: 16 }} column={2}>
          <Descriptions.Item label="充值余额">{fmt(wallet.balance)}</Descriptions.Item>
          <Descriptions.Item label="赠送余额">
            <div>{fmt(giftBalance)}</div>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              不可退款 / 不可提现
            </Typography.Text>
          </Descriptions.Item>
          <Descriptions.Item label="冻结中">
            现金 {fmt(wallet.held)} / 赠送 {fmt(giftHeld)}
          </Descriptions.Item>
          <Descriptions.Item label="总可用额">{fmt(totalAvailable)}</Descriptions.Item>
          <Descriptions.Item label="货币">{wallet.currency}</Descriptions.Item>
        </Descriptions>
      )}
      <Space style={{ marginBottom: 12 }}>
        <Button type="primary" onClick={() => setRechargeOpen(true)}>充值</Button>
        <Button onClick={openGift}>赠送</Button>
        <Button onClick={() => setAdjustOpen(true)}>调账</Button>
        <Button icon={<ReloadOutlined />} onClick={load} loading={loading} />
      </Space>
      <Tabs
        items={[
          {
            key: 'tx',
            label: '流水',
            children: (
              <Table
                rowKey="id"
                loading={loading}
                dataSource={txs}
                pagination={false}
                size="small"
                scroll={{ y: 380 }}
                columns={[
                  {
                    title: '类型', dataIndex: 'type', width: 90,
                    render: (v: string) => {
                      const m = TX_TYPE_MAP[v]
                      return m ? <Tag color={m.color}>{m.text}</Tag> : <Tag>{v}</Tag>
                    },
                  },
                  {
                    title: '金额', dataIndex: 'amount', width: 140,
                    render: (v: number, row: WalletTransaction) => (
                      <span style={{ color: v > 0 ? '#52c41a' : '#ff4d4f' }}>
                        {v > 0 ? '+' : ''}{fmt(v)}
                        {row.gift_amount ? (
                          <Typography.Text type="secondary" style={{ display: 'block', fontSize: 12 }}>
                            含赠金 {fmt(Math.abs(row.gift_amount))}
                          </Typography.Text>
                        ) : null}
                      </span>
                    ),
                  },
                  { title: '余额后', dataIndex: 'balance_after', width: 100, render: fmt },
                  { title: '备注', dataIndex: 'remark', ellipsis: true },
                  {
                    title: '时间',
                    dataIndex: 'created_at',
                    width: 160,
                    render: (v: string) => formatDateTime(v),
                  },
                ]}
              />
            ),
          },
          {
            key: 'gifts',
            label: '赠送批次',
            children: (
              <Table
                rowKey="id"
                loading={loading}
                dataSource={grants}
                pagination={false}
                size="small"
                scroll={{ y: 380 }}
                columns={[
                  { title: '金额', dataIndex: 'amount', width: 90, render: fmt },
                  { title: '剩余', dataIndex: 'remaining', width: 90, render: fmt },
                  {
                    title: '原因', dataIndex: 'reason', width: 100,
                    render: (v: string) => {
                      const m = GIFT_REASON_MAP[v]
                      return m ? <Tag color={m.color}>{m.text}</Tag> : <Tag>{v}</Tag>
                    },
                  },
                  {
                    title: '到期时间',
                    dataIndex: 'expires_at',
                    width: 160,
                    render: (v: string | null) => (v ? formatDateTime(v) : '永久有效'),
                  },
                  {
                    title: '状态', dataIndex: 'status', width: 80,
                    render: (v: string) => {
                      const m = GIFT_STATUS_MAP[v]
                      return m ? <Tag color={m.color}>{m.text}</Tag> : <Tag>{v}</Tag>
                    },
                  },
                  {
                    title: '操作人',
                    dataIndex: 'granted_by',
                    width: 140,
                    ellipsis: true,
                    render: (v: string) => operatorLabel(v),
                  },
                  {
                    title: '发放时间',
                    dataIndex: 'created_at',
                    width: 160,
                    render: (v: string) => formatDateTime(v),
                  },
                  {
                    title: '操作',
                    width: 80,
                    render: (_: unknown, row: GiftGrantOut) =>
                      row.status === 'active' && row.remaining > 0 ? (
                        <Button size="small" danger onClick={() => handleRevoke(row)}>
                          撤回
                        </Button>
                      ) : null,
                  },
                ]}
              />
            ),
          },
        ]}
      />

      <Modal
        open={rechargeOpen}
        title="充值"
        onOk={handleRecharge}
        onCancel={() => { setRechargeOpen(false); rechargeForm.resetFields() }}
        destroyOnHidden
      >
        <Typography.Text type="secondary" style={{ display: 'block', marginBottom: 8 }}>
          金额单位为最小货币单位（1 CNY = 10000）。
        </Typography.Text>
        <Form form={rechargeForm} layout="vertical">
          <Form.Item name="amount" label="充值金额" rules={[{ required: true }]}>
            <InputNumber min={1} style={{ width: '100%' }} placeholder="如 100000 = 10 CNY" />
          </Form.Item>
          <Form.Item name="remark" label="备注">
            <Input />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        open={adjustOpen}
        title="调账"
        onOk={handleAdjust}
        onCancel={() => { setAdjustOpen(false); adjustForm.resetFields() }}
        destroyOnHidden
      >
        <Typography.Text type="secondary" style={{ display: 'block', marginBottom: 8 }}>
          正数为增加余额，负数为减少余额。不能使余额变为负数。
        </Typography.Text>
        <Form form={adjustForm} layout="vertical">
          <Form.Item name="amount" label="调账金额" rules={[{ required: true }]}>
            <InputNumber style={{ width: '100%' }} placeholder="如 -10000 = -1 CNY" />
          </Form.Item>
          <Form.Item name="remark" label="备注">
            <Input />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        open={giftOpen}
        title="赠送余额"
        onOk={handleGift}
        onCancel={closeGift}
        confirmLoading={giftSubmitting}
        okButtonProps={{ disabled: giftSubmitting }}
        destroyOnHidden
      >
        <Typography.Text type="secondary" style={{ display: 'block', marginBottom: 8 }}>
          金额单位为最小货币单位（1 CNY = 10000）。赠送余额不可退款、不可提现。
        </Typography.Text>
        <Form form={giftForm} layout="vertical" initialValues={{ expiry: '30' }}>
          <Form.Item name="amount" label="赠送金额" rules={[{ required: true, message: '请输入金额' }]}>
            <InputNumber min={1} style={{ width: '100%' }} placeholder="如 100000 = 10 CNY" />
          </Form.Item>
          <Form.Item name="expiry" label="有效期" rules={[{ required: true }]}>
            <Radio.Group
              options={[
                { value: '30', label: '30 天' },
                { value: '90', label: '90 天' },
                { value: 'custom', label: '自定义日期' },
                { value: 'never', label: '永久有效' },
              ]}
            />
          </Form.Item>
          <Form.Item noStyle shouldUpdate={(prev, cur) => prev.expiry !== cur.expiry}>
            {({ getFieldValue }) =>
              getFieldValue('expiry') === 'custom' ? (
                <Form.Item
                  name="expires_at"
                  label="到期日期"
                  rules={[{ required: true, message: '请选择到期日期' }]}
                >
                  <DatePicker style={{ width: '100%' }} />
                </Form.Item>
              ) : null
            }
          </Form.Item>
          <Form.Item name="reason" label="原因分类" rules={[{ required: true, message: '请选择原因' }]}>
            <Select
              options={Object.entries(GIFT_REASON_MAP).map(([value, m]) => ({
                value,
                label: m.text,
              }))}
            />
          </Form.Item>
          <Form.Item name="remark" label="备注">
            <Input />
          </Form.Item>
        </Form>
      </Modal>
    </Modal>
  )
}

export function WalletsPage() {
  const { token } = useAdminAuth()
  const [users, setUsers] = useState<AdminUser[]>([])
  const [loading, setLoading] = useState(false)
  const [selectedUser, setSelectedUser] = useState<AdminUser | null>(null)

  if (!token) return null

  const load = async () => {
    setLoading(true)
    try {
      setUsers(await adminApi.listUsers(token))
    } catch (e) {
      message.error(e instanceof ApiError ? e.message : '加载失败')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { void load() }, [token])

  return (
    <Space direction="vertical" size="large" style={{ width: '100%' }}>
      <div>
        <Typography.Title level={3} style={{ marginBottom: 4 }}>钱包管理</Typography.Title>
        <Typography.Text type="secondary">为用户充值、赠送、调账，查看钱包流水与赠送批次。</Typography.Text>
      </div>
      <Card>
        <Table
          rowKey="id"
          loading={loading}
          dataSource={users}
          pagination={{ pageSize: 20 }}
          columns={[
            { title: '邮箱', dataIndex: 'email' },
            { title: '昵称', dataIndex: 'nickname', render: (v: string | null) => v ?? '—' },
            {
              title: '注册时间',
              dataIndex: 'created_at',
              width: 180,
              render: (v: string) => formatDateTime(v),
            },
            {
              title: '操作', width: 120,
              render: (_, row) => (
                <Button size="small" onClick={() => setSelectedUser(row)}>查看钱包</Button>
              ),
            },
          ]}
        />
      </Card>
      <WalletDetailModal
        token={token}
        user={selectedUser}
        users={users}
        onClose={() => setSelectedUser(null)}
      />
    </Space>
  )
}
