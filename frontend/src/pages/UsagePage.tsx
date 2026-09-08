import { useEffect, useState } from 'react'
import { Button, Card, Form, Input, Select, Space, Table, Typography, message } from 'antd'
import {
  FUND_SOURCE_OPTIONS,
  USAGE_SOURCE_OPTIONS,
  fetchUsage,
  formatUsageSource,
  type FundSource,
  type UsageLog,
  type UsageSource,
} from '../api/usage'
import { ApiError } from '../api/client'
import { useAuth } from '../auth/AuthContext'
import { formatDateTime } from '../utils/datetime'
import { formatMoney } from '../utils/money'

export function UsagePage() {
  const { token } = useAuth()
  const [items, setItems] = useState<UsageLog[]>([])
  const [loading, setLoading] = useState(false)
  const [model, setModel] = useState('')
  const [source, setSource] = useState<UsageSource | undefined>()
  const [fundSource, setFundSource] = useState<FundSource | undefined>()

  const load = async (opts?: {
    modelFilter?: string
    sourceFilter?: UsageSource
    fundSourceFilter?: FundSource
  }) => {
    if (!token) return
    setLoading(true)
    try {
      const data = await fetchUsage(token, {
        model: (opts?.modelFilter ?? model)?.trim() || undefined,
        source: opts?.sourceFilter ?? source,
        fund_source: opts?.fundSourceFilter ?? fundSource,
      })
      setItems(data)
    } catch (err) {
      message.error(err instanceof ApiError ? err.message : '加载失败')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    void load()
  }, [token])

  return (
    <Space direction="vertical" size="large" style={{ width: '100%' }}>
      <div>
        <Typography.Title level={3} style={{ marginBottom: 4 }}>
          用量
        </Typography.Title>
        <Typography.Paragraph type="secondary" style={{ marginBottom: 0 }}>
          按模型、来源与资金来源过滤查看调用次数、token 与费用明细（赠金抵扣 / 现金支付）。
        </Typography.Paragraph>
      </div>
      <Card>
        <Form
          layout="inline"
          onFinish={() =>
            load({ modelFilter: model, sourceFilter: source, fundSourceFilter: fundSource })
          }
          style={{ marginBottom: 16 }}
        >
          <Form.Item label="模型别名">
            <Input
              placeholder="如 gpt-4o"
              value={model}
              onChange={(e) => setModel(e.target.value)}
              allowClear
              style={{ width: 200 }}
            />
          </Form.Item>
          <Form.Item label="来源">
            <Select
              allowClear
              placeholder="全部来源"
              style={{ width: 160 }}
              value={source}
              onChange={(v) => setSource(v)}
              options={USAGE_SOURCE_OPTIONS}
            />
          </Form.Item>
          <Form.Item label="资金来源">
            <Select
              allowClear
              placeholder="全部"
              style={{ width: 140 }}
              value={fundSource}
              onChange={(v) => setFundSource(v)}
              options={FUND_SOURCE_OPTIONS}
            />
          </Form.Item>
          <Form.Item>
            <Button type="primary" htmlType="submit" loading={loading}>
              查询
            </Button>
          </Form.Item>
        </Form>
        <Table
          rowKey="id"
          loading={loading}
          dataSource={items}
          pagination={{ pageSize: 20 }}
          columns={[
            { title: '请求 ID', dataIndex: 'request_id', ellipsis: true },
            {
              title: '来源',
              dataIndex: 'source',
              width: 120,
              render: (v: string | null) => formatUsageSource(v),
            },
            { title: '输入 Token', dataIndex: 'input_tokens', width: 110 },
            { title: '输出 Token', dataIndex: 'output_tokens', width: 110 },
            { title: '缓存命中', dataIndex: 'cached_tokens', width: 110 },
            {
              title: '费用',
              dataIndex: 'charge_amount',
              render: (v: number) => formatMoney(v),
              width: 110,
            },
            {
              title: '赠金抵扣',
              dataIndex: 'gift_charge_amount',
              width: 110,
              render: (v: number | undefined) => formatMoney(v ?? 0),
            },
            {
              title: '现金支付',
              dataIndex: 'cash_charge_amount',
              width: 110,
              render: (v: number | undefined) => formatMoney(v ?? 0),
            },
            {
              title: '时间',
              dataIndex: 'created_at',
              render: (v: string) => formatDateTime(v),
            },
          ]}
        />
      </Card>
    </Space>
  )
}
