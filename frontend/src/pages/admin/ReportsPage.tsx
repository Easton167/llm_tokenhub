import { useEffect, useState } from 'react'
import {
  Button,
  Card,
  Col,
  DatePicker,
  Row,
  Select,
  Space,
  Statistic,
  Table,
  Tag,
  Tooltip,
  Typography,
  message,
  Tabs,
} from 'antd'
import dayjs, { type Dayjs } from 'dayjs'
import {
  adminApi,
  type ChannelShareItem,
  type DailyMetric,
  type GiftDashboard,
  type TopItem,
} from '../../api/admin'
import { ApiError } from '../../api/client'
import { useAdminAuth } from '../../auth/AdminAuthContext'
import { formatMoney } from '../../utils/money'

const { RangePicker } = DatePicker

function defaultRange(): [Dayjs, Dayjs] {
  return [dayjs().subtract(6, 'day'), dayjs()]
}

function rangeParams(range: [Dayjs, Dayjs]) {
  return {
    from_date: range[0].format('YYYY-MM-DD'),
    to_date: range[1].format('YYYY-MM-DD'),
  }
}

export function AdminReportsPage() {
  const { token } = useAdminAuth()
  const [range, setRange] = useState<[Dayjs, Dayjs]>(defaultRange())
  const [loading, setLoading] = useState(false)
  const [trend, setTrend] = useState<DailyMetric[]>([])
  const [topDim, setTopDim] = useState<'user' | 'org' | 'model'>('user')
  const [topItems, setTopItems] = useState<TopItem[]>([])
  const [channelItems, setChannelItems] = useState<ChannelShareItem[]>([])
  const [reconcile, setReconcile] = useState<{
    summary_revenue: number
    capture_revenue: number
    matched: boolean
    gift_matched?: boolean
    usage_gift_total?: number
    capture_gift_total?: number
  } | null>(null)
  const [gifts, setGifts] = useState<GiftDashboard | null>(null)

  const load = async () => {
    if (!token) return
    const params = rangeParams(range)
    const today = new Date().toISOString().slice(0, 10)
    setLoading(true)
    try {
      const [t, top, ch, rec, giftDash] = await Promise.all([
        adminApi.reportTrend(token, params),
        adminApi.reportTop(token, { ...params, dimension: topDim, limit: 10 }),
        adminApi.reportChannelShare(token, params),
        adminApi.reportReconcile(token, params),
        adminApi.reportGifts(token, { from_date: today, to_date: today }),
      ])
      setTrend(t.items)
      setTopItems(top.items)
      setChannelItems(ch.items)
      setReconcile({
        summary_revenue: rec.summary_revenue,
        capture_revenue: rec.capture_revenue,
        matched: rec.matched,
        gift_matched: rec.gift_matched,
        usage_gift_total: rec.usage_gift_total,
        capture_gift_total: rec.capture_gift_total,
      })
      setGifts(giftDash)
    } catch (err) {
      message.error(err instanceof ApiError ? err.message : '加载报表失败')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    void load()
  }, [token, topDim])

  const totals = trend.reduce(
    (acc, r) => ({
      revenue: acc.revenue + (r.cash_revenue_amount ?? r.revenue_amount),
      cost: acc.cost + r.cost_amount,
      profit: acc.profit + r.gross_profit,
      requests: acc.requests + r.request_count,
    }),
    { revenue: 0, cost: 0, profit: 0, requests: 0 },
  )

  return (
    <Space direction="vertical" size="large" style={{ width: '100%' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', gap: 16 }}>
        <div>
          <Typography.Title level={3} style={{ margin: 0 }}>
            运营报表
          </Typography.Title>
          <Typography.Paragraph type="secondary" style={{ marginBottom: 0 }}>
            现金收入 / 成本 / 毛利趋势与 Top 排行（UTC 日历日；收入不含赠金消耗）。
          </Typography.Paragraph>
        </div>
        <Space>
          <RangePicker value={range} onChange={(v) => v && setRange(v as [Dayjs, Dayjs])} />
          <Button type="primary" loading={loading} onClick={() => void load()}>
            查询
          </Button>
        </Space>
      </div>

      <Row gutter={16}>
        <Col xs={12} md={6}>
          <Card size="small" loading={loading}>
            <Statistic
              title={<Tooltip title="不含赠金消耗">现金收入 (CNY)</Tooltip>}
              value={formatMoney(totals.revenue)}
            />
          </Card>
        </Col>
        <Col xs={12} md={6}>
          <Card size="small" loading={loading}>
            <Statistic title="成本 (CNY)" value={formatMoney(totals.cost)} />
          </Card>
        </Col>
        <Col xs={12} md={6}>
          <Card size="small" loading={loading}>
            <Statistic
              title={<Tooltip title="现金收入 − 成本，不含赠金消耗">毛利 (CNY)</Tooltip>}
              value={formatMoney(totals.profit)}
            />
          </Card>
        </Col>
        <Col xs={12} md={6}>
          <Card size="small" loading={loading}>
            <Statistic title="请求数" value={totals.requests} />
          </Card>
        </Col>
      </Row>

      <Card size="small" title="赠金看板" loading={loading}>
        <Row gutter={[16, 16]}>
          <Col xs={12} md={6}>
            <Statistic
              title={<Tooltip title="全平台未消耗赠送余额（gift_balance + gift_held）">赠金余额敞口</Tooltip>}
              value={formatMoney(gifts?.gift_balance_total ?? 0)}
            />
          </Col>
          <Col xs={12} md={6}>
            <Statistic
              title="当日发放 / 撤回 / 过期"
              value={`${formatMoney(gifts?.granted_amount ?? 0)} / ${formatMoney(gifts?.revoked_amount ?? 0)} / ${formatMoney(gifts?.expired_amount ?? 0)}`}
            />
          </Col>
          <Col xs={12} md={6}>
            <Statistic title="当日赠金消耗" value={formatMoney(gifts?.gift_consumed ?? 0)} />
          </Col>
          <Col xs={12} md={6}>
            <Statistic title="赠金对应上游成本" value={formatMoney(gifts?.gift_cost ?? 0)} />
          </Col>
          <Col xs={12} md={6}>
            <Statistic
              title={<Tooltip title="累计赠金消耗 / 累计发放">核销率</Tooltip>}
              value={((gifts?.redemption_rate_bps ?? 0) / 100).toFixed(2)}
              suffix="%"
            />
          </Col>
        </Row>
      </Card>

      {reconcile && (
        <Card size="small">
          <Space>
            <Typography.Text>
              对账：汇总收入 {formatMoney(reconcile.summary_revenue)} / capture{' '}
              {formatMoney(reconcile.capture_revenue)}
            </Typography.Text>
            {reconcile.matched ? (
              <Tag color="green">匹配</Tag>
            ) : (
              <Tag color="red">不一致</Tag>
            )}
            {reconcile.gift_matched === false && (
              <Tag color="red">
                赠金拆分不一致 {formatMoney(reconcile.usage_gift_total ?? 0)} /{' '}
                {formatMoney(reconcile.capture_gift_total ?? 0)}
              </Tag>
            )}
          </Space>
        </Card>
      )}

      <Tabs
        items={[
          {
            key: 'trend',
            label: '趋势',
            children: (
              <Card>
                <Table
                  rowKey="summary_date"
                  loading={loading}
                  dataSource={trend}
                  pagination={false}
                  columns={[
                    { title: '日期', dataIndex: 'summary_date', width: 120 },
                    { title: '请求数', dataIndex: 'request_count', width: 90 },
                    {
                      title: '现金收入',
                      dataIndex: 'cash_revenue_amount',
                      render: (v: number, row: DailyMetric) => formatMoney(v ?? row.revenue_amount),
                    },
                    {
                      title: '赠金消耗',
                      dataIndex: 'gift_consumed',
                      render: (v: number) => formatMoney(v ?? 0),
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
                    { title: '输入 Token', dataIndex: 'input_tokens' },
                    { title: '输出 Token', dataIndex: 'output_tokens' },
                  ]}
                />
              </Card>
            ),
          },
          {
            key: 'top',
            label: 'Top 排行',
            children: (
              <Card
                extra={
                  <Select
                    value={topDim}
                    onChange={setTopDim}
                    style={{ width: 140 }}
                    options={[
                      { value: 'user', label: '按用户' },
                      { value: 'org', label: '按组织' },
                      { value: 'model', label: '按模型' },
                    ]}
                  />
                }
              >
                <Table
                  rowKey={(r) => r.dim_id ?? 'null'}
                  loading={loading}
                  dataSource={topItems}
                  pagination={false}
                  columns={[
                    {
                      title: '维度 ID',
                      dataIndex: 'dim_id',
                      render: (v: string | null) => v ?? '—（个人计费）',
                      ellipsis: true,
                    },
                    { title: '请求数', dataIndex: 'request_count', width: 90 },
                    {
                      title: '现金收入',
                      dataIndex: 'cash_revenue_amount',
                      render: (v: number | undefined, row: TopItem) =>
                        formatMoney(v ?? row.revenue_amount),
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
            ),
          },
          {
            key: 'channel',
            label: '渠道成本占比',
            children: (
              <Card>
                <Table
                  rowKey={(r) => r.channel_id ?? 'null'}
                  loading={loading}
                  dataSource={channelItems}
                  pagination={false}
                  columns={[
                    {
                      title: '渠道 ID',
                      dataIndex: 'channel_id',
                      render: (v: string | null) => v ?? '—',
                      ellipsis: true,
                    },
                    { title: '请求数', dataIndex: 'request_count', width: 90 },
                    {
                      title: '成本',
                      dataIndex: 'cost_amount',
                      render: (v: number) => formatMoney(v),
                    },
                    {
                      title: '成本占比',
                      dataIndex: 'cost_share_bps',
                      render: (v: number) => `${(v / 100).toFixed(2)}%`,
                    },
                    {
                      title: '现金收入',
                      dataIndex: 'cash_revenue_amount',
                      render: (v: number | undefined, row: ChannelShareItem) =>
                        formatMoney(v ?? row.revenue_amount),
                    },
                    {
                      title: '毛利',
                      dataIndex: 'gross_profit',
                      render: (v: number) => formatMoney(v),
                    },
                  ]}
                />
              </Card>
            ),
          },
        ]}
      />
    </Space>
  )
}
