import { useEffect, useState } from 'react'
import {
  Button,
  Card,
  Col,
  Row,
  Select,
  Space,
  Statistic,
  Table,
  Tag,
  Typography,
  message,
  Tabs,
} from 'antd'
import { ReloadOutlined } from '@ant-design/icons'
import { adminApi, type LLMModel, type RequestLog, type UsageSummary } from '../../api/admin'
import { ApiError } from '../../api/client'
import { useAdminAuth } from '../../auth/AdminAuthContext'
import { formatDateTime } from '../../utils/datetime'

function fmt(v: number) {
  return (v / 10_000).toFixed(4)
}

const STATUS_COLOR: Record<string, string> = {
  success: 'green',
  failed: 'red',
  partial: 'orange',
}

function SummaryTab({ token }: { token: string }) {
  const [data, setData] = useState<UsageSummary[]>([])
  const [models, setModels] = useState<LLMModel[]>([])
  const [loading, setLoading] = useState(false)
  const [groupBy, setGroupBy] = useState<'model' | 'user'>('model')

  const load = async () => {
    setLoading(true)
    try {
      const [s, m] = await Promise.all([
        adminApi.getUsageSummary(token, { group_by: groupBy }),
        adminApi.listModels(token),
      ])
      setData(s)
      setModels(m)
    } catch (e) {
      message.error(e instanceof ApiError ? e.message : '加载失败')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { void load() }, [groupBy])

  const totals = data.reduce(
    (acc, r) => ({
      requests: acc.requests + r.request_count,
      input: acc.input + r.input_tokens,
      output: acc.output + r.output_tokens,
      cost: acc.cost + r.cost_amount,
      charge: acc.charge + r.charge_amount,
    }),
    { requests: 0, input: 0, output: 0, cost: 0, charge: 0 },
  )

  const modelName = (id: string | null) =>
    id ? (models.find(m => m.id === id)?.alias ?? id.slice(0, 8)) : '—'

  return (
    <>
      <Row gutter={16} style={{ marginBottom: 16 }}>
        <Col span={4}><Card size="small"><Statistic title="总请求数" value={totals.requests} /></Card></Col>
        <Col span={5}><Card size="small"><Statistic title="输入 Token" value={totals.input.toLocaleString()} /></Card></Col>
        <Col span={5}><Card size="small"><Statistic title="输出 Token" value={totals.output.toLocaleString()} /></Card></Col>
        <Col span={5}><Card size="small"><Statistic title="成本（CNY）" value={fmt(totals.cost)} /></Card></Col>
        <Col span={5}><Card size="small"><Statistic title="收入（CNY）" value={fmt(totals.charge)} /></Card></Col>
      </Row>
      <div style={{ display: 'flex', gap: 8, alignItems: 'center', marginBottom: 12 }}>
        <Select
          value={groupBy}
          onChange={setGroupBy}
          options={[{ value: 'model', label: '按模型' }, { value: 'user', label: '按用户' }]}
          style={{ width: 120 }}
        />
        <Button icon={<ReloadOutlined />} onClick={load} loading={loading} />
      </div>
      <Table
        rowKey={(r) => r.model_id ?? r.user_id ?? 'unknown'}
        loading={loading}
        dataSource={data}
        pagination={false}
        columns={[
          {
            title: groupBy === 'model' ? '模型' : '用户 ID',
            render: (_, r) => groupBy === 'model'
              ? modelName(r.model_id)
              : (r.user_id?.slice(0, 8) ?? '—'),
          },
          { title: '请求数', dataIndex: 'request_count', sorter: (a, b) => a.request_count - b.request_count },
          { title: '输入 Token', dataIndex: 'input_tokens', sorter: (a, b) => a.input_tokens - b.input_tokens },
          { title: '输出 Token', dataIndex: 'output_tokens', sorter: (a, b) => a.output_tokens - b.output_tokens },
          { title: '成本', dataIndex: 'cost_amount', render: fmt, sorter: (a, b) => a.cost_amount - b.cost_amount },
          { title: '收入', dataIndex: 'charge_amount', render: fmt, sorter: (a, b) => a.charge_amount - b.charge_amount },
        ]}
      />
    </>
  )
}

function RequestLogsTab({ token }: { token: string }) {
  const [logs, setLogs] = useState<RequestLog[]>([])
  const [loading, setLoading] = useState(false)
  const [status, setStatus] = useState<string | undefined>()
  const [offset, setOffset] = useState(0)
  const PAGE = 50

  const load = async (off = 0) => {
    setLoading(true)
    try {
      setLogs(await adminApi.listRequestLogs(token, { limit: PAGE, offset: off, status }))
      setOffset(off)
    } catch (e) {
      message.error(e instanceof ApiError ? e.message : '加载失败')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { void load(0) }, [status])

  return (
    <>
      <Space style={{ marginBottom: 12 }}>
        <Select
          allowClear
          placeholder="状态过滤"
          value={status}
          onChange={v => setStatus(v)}
          style={{ width: 140 }}
          options={[
            { value: 'success', label: '成功' },
            { value: 'failed', label: '失败' },
            { value: 'partial', label: '部分成功' },
          ]}
        />
        <Button icon={<ReloadOutlined />} onClick={() => load(0)} loading={loading} />
      </Space>
      <Table
        rowKey="id"
        loading={loading}
        dataSource={logs}
        pagination={false}
        size="small"
        columns={[
          { title: 'Request ID', dataIndex: 'request_id', ellipsis: true, width: 220 },
          { title: '用户', dataIndex: 'user_id', render: (v: string) => v?.slice(0, 8), width: 80 },
          { title: '模型', dataIndex: 'model_id', render: (v: string) => v?.slice(0, 8), width: 80 },
          {
            title: '状态', dataIndex: 'status', width: 80,
            render: (v: string) => <Tag color={STATUS_COLOR[v] ?? 'default'}>{v}</Tag>,
          },
          { title: 'HTTP', dataIndex: 'http_status', width: 60 },
          { title: '延迟(ms)', dataIndex: 'latency_ms', width: 80 },
          { title: '流式', dataIndex: 'is_stream', width: 60, render: (v: boolean) => v ? '是' : '否' },
          {
            title: '时间',
            dataIndex: 'created_at',
            width: 160,
            render: (v: string) => formatDateTime(v),
          },
        ]}
        footer={() => (
          <Space>
            <Button size="small" disabled={offset === 0} onClick={() => load(Math.max(0, offset - PAGE))}>上一页</Button>
            <Button size="small" disabled={logs.length < PAGE} onClick={() => load(offset + PAGE)}>下一页</Button>
          </Space>
        )}
      />
    </>
  )
}

export function StatsPage() {
  const { token } = useAdminAuth()
  if (!token) return null

  return (
    <Space direction="vertical" size="large" style={{ width: '100%' }}>
      <div>
        <Typography.Title level={3} style={{ marginBottom: 4 }}>运营数据</Typography.Title>
        <Typography.Text type="secondary">用量汇总与请求日志查询。</Typography.Text>
      </div>
      <Card>
        <Tabs
          items={[
            { key: 'summary', label: '用量汇总', children: <SummaryTab token={token} /> },
            { key: 'logs', label: '请求日志', children: <RequestLogsTab token={token} /> },
          ]}
        />
      </Card>
    </Space>
  )
}
