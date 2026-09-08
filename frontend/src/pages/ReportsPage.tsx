import { useEffect, useState } from 'react'
import {
  Button,
  Card,
  DatePicker,
  Form,
  Select,
  Space,
  Table,
  Tabs,
  Typography,
  message,
} from 'antd'
import dayjs, { type Dayjs } from 'dayjs'
import { fetchUsageDaily } from '../api/usage'
import {
  fetchOrgDailyReport,
  fetchOrgMemberShareReport,
  listOrgs,
  type DailyMetric,
  type OrgMemberShareItem,
  type OrgWithRole,
} from '../api/orgs'
import { ApiError } from '../api/client'
import { useAuth } from '../auth/AuthContext'
import { formatMoney } from '../utils/money'

const { RangePicker } = DatePicker

function defaultRange(): [Dayjs, Dayjs] {
  return [dayjs().subtract(6, 'day'), dayjs()]
}

function cashConsumed(row: DailyMetric): number {
  return row.cash_charge_amount ?? row.cash_revenue_amount ?? 0
}

function giftConsumed(row: DailyMetric): number {
  return row.gift_charge_amount ?? row.gift_consumed ?? 0
}

const metricColumns = [
  { title: '日期 (UTC)', dataIndex: 'summary_date', width: 120 },
  { title: '请求数', dataIndex: 'request_count', width: 90 },
  { title: '输入 Token', dataIndex: 'input_tokens' },
  { title: '输出 Token', dataIndex: 'output_tokens' },
  {
    title: '计费总额',
    dataIndex: 'revenue_amount',
    render: (v: number) => formatMoney(v),
  },
  {
    title: '现金消费',
    render: (_: unknown, row: DailyMetric) => formatMoney(cashConsumed(row)),
  },
  {
    title: '赠金消费',
    render: (_: unknown, row: DailyMetric) => formatMoney(giftConsumed(row)),
  },
  {
    title: '成本',
    dataIndex: 'cost_amount',
    render: (v: number) => formatMoney(v),
  },
  {
    title: '毛利',
    dataIndex: 'gross_profit',
    render: (v: number) => formatMoney(v),
  },
]

export function ReportsPage() {
  const { token } = useAuth()
  const [orgs, setOrgs] = useState<OrgWithRole[]>([])
  const [personalItems, setPersonalItems] = useState<DailyMetric[]>([])
  const [orgDailyItems, setOrgDailyItems] = useState<DailyMetric[]>([])
  const [memberItems, setMemberItems] = useState<OrgMemberShareItem[]>([])
  const [loading, setLoading] = useState(false)
  const [personalForm] = Form.useForm()
  const [orgForm] = Form.useForm()

  useEffect(() => {
    if (!token) return
    listOrgs(token)
      .then(setOrgs)
      .catch((err) => {
        message.error(err instanceof ApiError ? err.message : '加载组织失败')
      })
  }, [token])

  const loadPersonal = async () => {
    if (!token) return
    const values = await personalForm.validateFields()
    const [from, to] = values.range as [Dayjs, Dayjs]
    setLoading(true)
    try {
      const report = await fetchUsageDaily(token, {
        from_date: from.format('YYYY-MM-DD'),
        to_date: to.format('YYYY-MM-DD'),
      })
      setPersonalItems(report.items)
    } catch (err) {
      message.error(err instanceof ApiError ? err.message : '查询失败')
    } finally {
      setLoading(false)
    }
  }

  const loadOrg = async () => {
    if (!token) return
    const values = await orgForm.validateFields()
    const [from, to] = values.range as [Dayjs, Dayjs]
    const params = {
      from_date: from.format('YYYY-MM-DD'),
      to_date: to.format('YYYY-MM-DD'),
    }
    setLoading(true)
    try {
      const [daily, members] = await Promise.all([
        fetchOrgDailyReport(token, values.org_id, params),
        fetchOrgMemberShareReport(token, values.org_id, params).catch(() => null),
      ])
      setOrgDailyItems(daily.items)
      setMemberItems(members?.items ?? [])
      if (!members) {
        message.info('成员分摊仅 Owner/Admin 可见；已展示按天汇总')
      }
    } catch (err) {
      message.error(err instanceof ApiError ? err.message : '查询失败')
    } finally {
      setLoading(false)
    }
  }

  return (
    <Space direction="vertical" size="large" style={{ width: '100%' }}>
      <div>
        <Typography.Title level={3} style={{ marginBottom: 4 }}>
          用量报表
        </Typography.Title>
        <Typography.Paragraph type="secondary" style={{ marginBottom: 0 }}>
          按 UTC 日历日汇总；查询含当天时会刷新当日数据。计费总额含赠金抵扣；现金消费与赠金消费分列。组织成员分摊需
          Owner 或 Admin。
        </Typography.Paragraph>
      </div>

      <Tabs
        items={[
          {
            key: 'personal',
            label: '个人按天',
            children: (
              <Card>
                <Form
                  form={personalForm}
                  layout="inline"
                  initialValues={{ range: defaultRange() }}
                  onFinish={() => void loadPersonal()}
                  style={{ marginBottom: 16 }}
                >
                  <Form.Item
                    name="range"
                    label="日期范围"
                    rules={[{ required: true, message: '请选择日期' }]}
                  >
                    <RangePicker />
                  </Form.Item>
                  <Form.Item>
                    <Button type="primary" htmlType="submit" loading={loading}>
                      查询
                    </Button>
                  </Form.Item>
                </Form>
                <Table
                  rowKey="summary_date"
                  loading={loading}
                  dataSource={personalItems}
                  pagination={false}
                  columns={metricColumns}
                />
              </Card>
            ),
          },
          {
            key: 'org',
            label: '组织按天 / 成员分摊',
            children: (
              <Space direction="vertical" size="middle" style={{ width: '100%' }}>
                <Card>
                  <Form
                    form={orgForm}
                    layout="inline"
                    initialValues={{ range: defaultRange() }}
                    onFinish={() => void loadOrg()}
                    style={{ marginBottom: 16 }}
                  >
                    <Form.Item
                      name="org_id"
                      label="组织"
                      rules={[{ required: true, message: '请选择组织' }]}
                    >
                      <Select
                        style={{ width: 220 }}
                        placeholder="选择组织"
                        options={orgs.map((o) => ({
                          value: o.id,
                          label: `${o.name} (${o.my_org_role})`,
                        }))}
                      />
                    </Form.Item>
                    <Form.Item
                      name="range"
                      label="日期范围"
                      rules={[{ required: true, message: '请选择日期' }]}
                    >
                      <RangePicker />
                    </Form.Item>
                    <Form.Item>
                      <Button type="primary" htmlType="submit" loading={loading}>
                        查询
                      </Button>
                    </Form.Item>
                  </Form>
                  <Typography.Title level={5}>按天汇总</Typography.Title>
                  <Table
                    rowKey="summary_date"
                    loading={loading}
                    dataSource={orgDailyItems}
                    pagination={false}
                    columns={metricColumns}
                    style={{ marginBottom: 24 }}
                  />
                  <Typography.Title level={5}>成员分摊</Typography.Title>
                  <Table
                    rowKey="user_id"
                    loading={loading}
                    dataSource={memberItems}
                    pagination={false}
                    columns={[
                      { title: '用户 ID', dataIndex: 'user_id', ellipsis: true },
                      { title: '请求数', dataIndex: 'request_count', width: 90 },
                      { title: '输入 Token', dataIndex: 'input_tokens' },
                      { title: '输出 Token', dataIndex: 'output_tokens' },
                      {
                        title: '计费总额',
                        dataIndex: 'revenue_amount',
                        render: (v: number) => formatMoney(v),
                      },
                      {
                        title: '现金消费',
                        render: (_: unknown, row: OrgMemberShareItem) =>
                          formatMoney(row.cash_charge_amount ?? row.cash_revenue_amount ?? 0),
                      },
                      {
                        title: '赠金消费',
                        render: (_: unknown, row: OrgMemberShareItem) =>
                          formatMoney(row.gift_charge_amount ?? row.gift_consumed ?? 0),
                      },
                      {
                        title: '成本',
                        dataIndex: 'cost_amount',
                        render: (v: number) => formatMoney(v),
                      },
                      {
                        title: '毛利',
                        dataIndex: 'gross_profit',
                        render: (v: number) => formatMoney(v),
                      },
                    ]}
                  />
                </Card>
              </Space>
            ),
          },
        ]}
      />
    </Space>
  )
}
