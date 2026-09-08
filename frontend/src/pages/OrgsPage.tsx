import { useCallback, useEffect, useState } from 'react'
import {
  Button,
  Card,
  Col,
  Descriptions,
  Form,
  Input,
  InputNumber,
  Modal,
  Row,
  Select,
  Space,
  Statistic,
  Switch,
  Table,
  Tag,
  Typography,
  message,
} from 'antd'
import { PlusOutlined, ReloadOutlined } from '@ant-design/icons'
import { Link } from 'react-router-dom'
import {
  createOrg,
  fetchOrgTransactions,
  fetchOrgWallet,
  getOrg,
  inviteOrgMember,
  listOrgs,
  removeOrgMember,
  updateOrg,
  updateOrgMember,
  type BillingFallback,
  type OrgDetail,
  type OrgMember,
  type OrgRole,
  type OrgWithRole,
  type QuotaPeriod,
} from '../api/orgs'
import {
  formatTxType,
  walletTotalAvailable,
  type Wallet,
  type WalletTransaction,
} from '../api/wallet'
import { ApiError } from '../api/client'
import { useAuth } from '../auth/AuthContext'
import { formatDateTime } from '../utils/datetime'
import { formatMoney } from '../utils/money'

const ROLE_COLOR: Record<string, string> = {
  owner: 'gold',
  admin: 'blue',
  member: 'default',
}

function canManage(role: OrgRole) {
  return role === 'owner' || role === 'admin'
}

export function OrgsPage() {
  const { token } = useAuth()
  const [orgs, setOrgs] = useState<OrgWithRole[]>([])
  const [loading, setLoading] = useState(false)
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [detail, setDetail] = useState<OrgDetail | null>(null)
  const [wallet, setWallet] = useState<Wallet | null>(null)
  const [txs, setTxs] = useState<WalletTransaction[]>([])
  const [detailLoading, setDetailLoading] = useState(false)
  const [createOpen, setCreateOpen] = useState(false)
  const [inviteOpen, setInviteOpen] = useState(false)
  const [editMember, setEditMember] = useState<OrgMember | null>(null)
  const [createForm] = Form.useForm()
  const [inviteForm] = Form.useForm()
  const [memberForm] = Form.useForm()
  const [settingsForm] = Form.useForm()

  const currentRole: OrgRole | undefined =
    orgs.find((o) => o.id === selectedId)?.my_org_role

  const reloadList = useCallback(async () => {
    if (!token) return
    setLoading(true)
    try {
      const list = await listOrgs(token)
      setOrgs(list)
      setSelectedId((prev) => prev ?? (list[0]?.id ?? null))
    } catch (err) {
      message.error(err instanceof ApiError ? err.message : '加载组织失败')
    } finally {
      setLoading(false)
    }
  }, [token])

  const reloadDetail = useCallback(async () => {
    if (!token || !selectedId) {
      setDetail(null)
      setWallet(null)
      setTxs([])
      return
    }
    setDetailLoading(true)
    try {
      const d = await getOrg(token, selectedId)
      setDetail(d)
      settingsForm.setFieldsValue({
        name: d.name,
        billing_fallback: d.billing_fallback,
      })
      const role = orgs.find((o) => o.id === selectedId)?.my_org_role
      if (role && canManage(role)) {
        try {
          const [w, t] = await Promise.all([
            fetchOrgWallet(token, selectedId),
            fetchOrgTransactions(token, selectedId),
          ])
          setWallet(w)
          setTxs(t)
        } catch {
          setWallet(null)
          setTxs([])
        }
      } else {
        setWallet(null)
        setTxs([])
      }
    } catch (err) {
      message.error(err instanceof ApiError ? err.message : '加载组织详情失败')
    } finally {
      setDetailLoading(false)
    }
  }, [token, selectedId, orgs, settingsForm])

  useEffect(() => {
    void reloadList()
  }, [token])

  useEffect(() => {
    void reloadDetail()
  }, [selectedId])

  useEffect(() => {
    if (editMember) {
      memberForm.setFieldsValue({
        org_role: editMember.org_role,
        can_consume_org_wallet: editMember.can_consume_org_wallet,
        quota_period: editMember.quota?.quota_period ?? 'none',
        quota_limit: editMember.quota?.quota_limit ?? 0,
      })
    }
  }, [editMember, memberForm])

  return (
    <Space direction="vertical" size="large" style={{ width: '100%' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', gap: 16 }}>
        <div>
          <Typography.Title level={3} style={{ margin: 0 }}>
            我的组织
          </Typography.Title>
          <Typography.Paragraph type="secondary" style={{ marginBottom: 0 }}>
            创建组织、邀请成员、配置额度与组织钱包。邀请需填写对方已注册用户的
            user_id。
          </Typography.Paragraph>
        </div>
        <Space>
          <Button icon={<ReloadOutlined />} onClick={() => void reloadList()}>
            刷新
          </Button>
          <Button type="primary" icon={<PlusOutlined />} onClick={() => setCreateOpen(true)}>
            创建组织
          </Button>
        </Space>
      </div>

      <Row gutter={16}>
        <Col xs={24} lg={8}>
          <Card title="组织列表" loading={loading}>
            <Table
              rowKey="id"
              size="small"
              pagination={false}
              dataSource={orgs}
              rowSelection={{
                type: 'radio',
                selectedRowKeys: selectedId ? [selectedId] : [],
                onChange: (keys) => setSelectedId(String(keys[0] || '')),
              }}
              onRow={(row) => ({
                onClick: () => setSelectedId(row.id),
                style: { cursor: 'pointer' },
              })}
              columns={[
                { title: '名称', dataIndex: 'name' },
                {
                  title: '我的角色',
                  dataIndex: 'my_org_role',
                  width: 90,
                  render: (v: string) => <Tag color={ROLE_COLOR[v]}>{v}</Tag>,
                },
                {
                  title: '状态',
                  dataIndex: 'status',
                  width: 70,
                  render: (v: number) =>
                    v === 0 ? <Tag color="green">活跃</Tag> : <Tag>停用</Tag>,
                },
              ]}
            />
          </Card>
        </Col>

        <Col xs={24} lg={16}>
          {!selectedId ? (
            <Card>
              <Typography.Text type="secondary">请选择或创建一个组织</Typography.Text>
            </Card>
          ) : (
            <Space direction="vertical" size="middle" style={{ width: '100%' }}>
              <Card title="组织信息" loading={detailLoading}>
                {detail && (
                  <>
                    <Descriptions column={2} size="small" style={{ marginBottom: 16 }}>
                      <Descriptions.Item label="ID">{detail.id}</Descriptions.Item>
                      <Descriptions.Item label="Owner">{detail.owner_user_id}</Descriptions.Item>
                      <Descriptions.Item label="余额兜底">
                        {detail.billing_fallback === 'personal' ? '回退个人' : '拒绝'}
                      </Descriptions.Item>
                      <Descriptions.Item label="我的角色">
                        <Tag color={ROLE_COLOR[currentRole || '']}>{currentRole}</Tag>
                      </Descriptions.Item>
                    </Descriptions>
                    {currentRole === 'owner' && (
                      <Form
                        form={settingsForm}
                        layout="inline"
                        onFinish={async (values) => {
                          if (!token || !selectedId) return
                          try {
                            await updateOrg(token, selectedId, {
                              name: values.name,
                              billing_fallback: values.billing_fallback as BillingFallback,
                            })
                            message.success('已更新')
                            await reloadList()
                            await reloadDetail()
                          } catch (err) {
                            message.error(
                              err instanceof ApiError ? err.message : '更新失败',
                            )
                          }
                        }}
                      >
                        <Form.Item name="name" label="名称" rules={[{ required: true }]}>
                          <Input style={{ width: 160 }} />
                        </Form.Item>
                        <Form.Item name="billing_fallback" label="余额不足">
                          <Select
                            style={{ width: 140 }}
                            options={[
                              { value: 'deny', label: '拒绝请求' },
                              { value: 'personal', label: '回退个人钱包' },
                            ]}
                          />
                        </Form.Item>
                        <Form.Item>
                          <Button type="primary" htmlType="submit">
                            保存
                          </Button>
                        </Form.Item>
                      </Form>
                    )}
                  </>
                )}
              </Card>

              {currentRole && canManage(currentRole) && (
                <Card
                  title="组织钱包"
                  loading={detailLoading}
                  extra={<Link to="/recharge">去充值</Link>}
                >
                  <Row gutter={16} style={{ marginBottom: 16 }}>
                    <Col xs={24} sm={12} md={6}>
                      <Statistic
                        title="充值余额"
                        value={formatMoney(wallet?.balance ?? 0)}
                        suffix={wallet?.currency || 'CNY'}
                      />
                    </Col>
                    <Col xs={24} sm={12} md={6}>
                      <Statistic
                        title="赠送余额"
                        value={formatMoney(wallet?.gift_balance ?? 0)}
                        suffix={wallet?.currency || 'CNY'}
                      />
                      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                        不可退款、不可提现
                      </Typography.Text>
                    </Col>
                    <Col xs={24} sm={12} md={6}>
                      <Statistic
                        title="总可用额"
                        value={formatMoney(walletTotalAvailable(wallet))}
                        suffix={wallet?.currency || 'CNY'}
                      />
                    </Col>
                    <Col xs={24} sm={12} md={6}>
                      <Statistic
                        title="冻结中"
                        value={formatMoney((wallet?.held ?? 0) + (wallet?.gift_held ?? 0))}
                      />
                      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                        现金 {formatMoney(wallet?.held ?? 0)} / 赠送{' '}
                        {formatMoney(wallet?.gift_held ?? 0)}
                      </Typography.Text>
                    </Col>
                  </Row>
                  <Table
                    rowKey="id"
                    size="small"
                    dataSource={txs}
                    pagination={{ pageSize: 8 }}
                    columns={[
                      {
                        title: '类型',
                        dataIndex: 'type',
                        width: 100,
                        render: (v: string) => {
                          const m = formatTxType(v)
                          return <Tag color={m.color}>{m.text}</Tag>
                        },
                      },
                      {
                        title: '金额',
                        dataIndex: 'amount',
                        render: (v: number, row: WalletTransaction) => (
                          <span>
                            {formatMoney(v)}
                            {row.gift_amount ? (
                              <Typography.Text
                                type="secondary"
                                style={{ display: 'block', fontSize: 12 }}
                              >
                                含赠金 {formatMoney(Math.abs(row.gift_amount))}
                              </Typography.Text>
                            ) : null}
                          </span>
                        ),
                      },
                      {
                        title: '充值余额后',
                        dataIndex: 'balance_after',
                        render: (v: number) => formatMoney(v),
                      },
                      { title: '备注', dataIndex: 'remark', ellipsis: true },
                      {
                        title: '时间',
                        dataIndex: 'created_at',
                        render: (v: string) => formatDateTime(v),
                      },
                    ]}
                  />
                </Card>
              )}

              <Card
                title="成员与额度"
                loading={detailLoading}
                extra={
                  currentRole && canManage(currentRole) ? (
                    <Button type="primary" size="small" onClick={() => setInviteOpen(true)}>
                      邀请成员
                    </Button>
                  ) : null
                }
              >
                <Table
                  rowKey="id"
                  size="small"
                  dataSource={detail?.members || []}
                  pagination={false}
                  columns={[
                    {
                      title: '用户 ID',
                      dataIndex: 'user_id',
                      ellipsis: true,
                    },
                    {
                      title: '角色',
                      dataIndex: 'org_role',
                      width: 90,
                      render: (v: string) => <Tag color={ROLE_COLOR[v]}>{v}</Tag>,
                    },
                    {
                      title: '可用组织钱包',
                      dataIndex: 'can_consume_org_wallet',
                      width: 120,
                      render: (v: boolean) => (v ? '是' : '否'),
                    },
                    {
                      title: '额度',
                      render: (_, row: OrgMember) => {
                        const q = row.quota
                        if (!q || q.quota_period === 'none') return '不限'
                        return `${q.quota_period} ${formatMoney(q.quota_used)} / ${formatMoney(q.quota_limit)}`
                      },
                    },
                    {
                      title: '操作',
                      width: 160,
                      render: (_, row) =>
                        currentRole && canManage(currentRole) ? (
                          <Space>
                            <Button size="small" onClick={() => setEditMember(row)}>
                              编辑
                            </Button>
                            {row.org_role !== 'owner' && (
                              <Button
                                size="small"
                                danger
                                onClick={() => {
                                  Modal.confirm({
                                    title: '移除该成员？',
                                    onOk: async () => {
                                      if (!token || !selectedId) return
                                      await removeOrgMember(token, selectedId, row.id)
                                      message.success('已移除')
                                      await reloadDetail()
                                    },
                                  })
                                }}
                              >
                                移除
                              </Button>
                            )}
                          </Space>
                        ) : null,
                    },
                  ]}
                />
              </Card>
            </Space>
          )}
        </Col>
      </Row>

      <Modal
        open={createOpen}
        title="创建组织"
        okText="创建"
        onCancel={() => {
          setCreateOpen(false)
          createForm.resetFields()
        }}
        onOk={async () => {
          if (!token) return
          const values = await createForm.validateFields()
          try {
            const org = await createOrg(token, {
              name: values.name.trim(),
              billing_fallback: values.billing_fallback,
            })
            message.success('组织已创建')
            setCreateOpen(false)
            createForm.resetFields()
            await reloadList()
            setSelectedId(org.id)
          } catch (err) {
            message.error(err instanceof ApiError ? err.message : '创建失败')
            throw err
          }
        }}
      >
        <Form
          form={createForm}
          layout="vertical"
          initialValues={{ billing_fallback: 'deny' }}
        >
          <Form.Item
            name="name"
            label="组织名称"
            rules={[{ required: true, message: '请输入名称' }]}
          >
            <Input placeholder="我的团队" />
          </Form.Item>
          <Form.Item
            name="billing_fallback"
            label="组织余额不足时"
            extra="personal=回退扣个人钱包；deny=直接拒绝"
          >
            <Select
              options={[
                { value: 'deny', label: '拒绝请求' },
                { value: 'personal', label: '回退个人钱包' },
              ]}
            />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        open={inviteOpen}
        title="邀请成员"
        okText="添加"
        onCancel={() => {
          setInviteOpen(false)
          inviteForm.resetFields()
        }}
        onOk={async () => {
          if (!token || !selectedId) return
          const values = await inviteForm.validateFields()
          try {
            await inviteOrgMember(token, selectedId, {
              user_id: values.user_id.trim(),
              org_role: values.org_role as OrgRole,
            })
            message.success('已添加成员')
            setInviteOpen(false)
            inviteForm.resetFields()
            await reloadDetail()
          } catch (err) {
            message.error(err instanceof ApiError ? err.message : '邀请失败')
            throw err
          }
        }}
      >
        <Form form={inviteForm} layout="vertical" initialValues={{ org_role: 'member' }}>
          <Form.Item
            name="user_id"
            label="用户 ID"
            rules={[{ required: true, message: '请输入已注册用户的 UUID' }]}
            extra="对方需先注册；可在管理端用户列表查看 user_id"
          >
            <Input placeholder="uuid" />
          </Form.Item>
          <Form.Item name="org_role" label="角色">
            <Select
              options={[
                { value: 'member', label: 'member' },
                { value: 'admin', label: 'admin' },
              ]}
            />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        open={!!editMember}
        title="编辑成员"
        okText="保存"
        onCancel={() => setEditMember(null)}
        onOk={async () => {
          if (!token || !selectedId || !editMember) return
          const values = await memberForm.validateFields()
          try {
            await updateOrgMember(token, selectedId, editMember.id, {
              org_role: values.org_role as OrgRole,
              can_consume_org_wallet: values.can_consume_org_wallet,
              quota_period: values.quota_period as QuotaPeriod,
              quota_limit: values.quota_limit ?? 0,
            })
            message.success('已更新')
            setEditMember(null)
            await reloadDetail()
          } catch (err) {
            message.error(err instanceof ApiError ? err.message : '更新失败')
            throw err
          }
        }}
      >
        <Form form={memberForm} layout="vertical">
          <Form.Item name="org_role" label="角色">
            <Select
              options={[
                { value: 'owner', label: 'owner' },
                { value: 'admin', label: 'admin' },
                { value: 'member', label: 'member' },
              ]}
            />
          </Form.Item>
          <Form.Item
            name="can_consume_org_wallet"
            label="允许消费组织钱包"
            valuePropName="checked"
          >
            <Switch />
          </Form.Item>
          <Form.Item name="quota_period" label="额度周期">
            <Select
              options={[
                { value: 'none', label: '不限' },
                { value: 'daily', label: '按天' },
                { value: 'monthly', label: '按月' },
              ]}
            />
          </Form.Item>
          <Form.Item
            name="quota_limit"
            label="额度上限（最小货币单位）"
            extra="1 元 = 10000；none 时可填 0"
          >
            <InputNumber min={0} style={{ width: '100%' }} />
          </Form.Item>
        </Form>
      </Modal>
    </Space>
  )
}
