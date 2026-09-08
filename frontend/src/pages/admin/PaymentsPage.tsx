import { useEffect, useState } from 'react'
import {
  Button,
  Card,
  Form,
  Input,
  Select,
  Space,
  Table,
  Tag,
  Typography,
  message,
} from 'antd'
import { ReloadOutlined } from '@ant-design/icons'
import { adminApi, type AdminPaymentOrder } from '../../api/admin'
import { ApiError } from '../../api/client'
import { useAdminAuth } from '../../auth/AdminAuthContext'
import { formatDateTime } from '../../utils/datetime'
import { formatMoney } from '../../utils/money'

const STATUS_COLOR: Record<string, string> = {
  created: 'blue',
  paid: 'green',
  expired: 'default',
  failed: 'red',
}

export function AdminPaymentsPage() {
  const { token } = useAdminAuth()
  const [items, setItems] = useState<AdminPaymentOrder[]>([])
  const [loading, setLoading] = useState(false)
  const [form] = Form.useForm()

  const load = async (filters?: {
    status?: string
    provider?: string
    user_id?: string
    org_id?: string
  }) => {
    if (!token) return
    setLoading(true)
    try {
      setItems(
        await adminApi.listPayments(token, {
          ...filters,
          limit: 100,
        }),
      )
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
          支付单查询
        </Typography.Title>
        <Typography.Paragraph type="secondary" style={{ marginBottom: 0 }}>
          按用户、组织、状态与渠道筛选在线充值订单。
        </Typography.Paragraph>
      </div>

      <Card>
        <Form
          form={form}
          layout="inline"
          style={{ marginBottom: 16, rowGap: 8 }}
          onFinish={(values) =>
            void load({
              status: values.status || undefined,
              provider: values.provider || undefined,
              user_id: values.user_id?.trim() || undefined,
              org_id: values.org_id?.trim() || undefined,
            })
          }
        >
          <Form.Item name="status" label="状态">
            <Select
              allowClear
              style={{ width: 120 }}
              options={['created', 'paid', 'expired', 'failed'].map((v) => ({
                value: v,
                label: v,
              }))}
            />
          </Form.Item>
          <Form.Item name="provider" label="渠道">
            <Select
              allowClear
              style={{ width: 120 }}
              options={[
                { value: 'mock', label: 'mock' },
                { value: 'stripe', label: 'stripe' },
              ]}
            />
          </Form.Item>
          <Form.Item name="user_id" label="用户 ID">
            <Input allowClear style={{ width: 200 }} placeholder="uuid" />
          </Form.Item>
          <Form.Item name="org_id" label="组织 ID">
            <Input allowClear style={{ width: 200 }} placeholder="uuid" />
          </Form.Item>
          <Form.Item>
            <Space>
              <Button type="primary" htmlType="submit" loading={loading}>
                查询
              </Button>
              <Button
                icon={<ReloadOutlined />}
                onClick={() => {
                  form.resetFields()
                  void load()
                }}
              />
            </Space>
          </Form.Item>
        </Form>

        <Table
          rowKey="id"
          loading={loading}
          dataSource={items}
          pagination={{ pageSize: 20 }}
          scroll={{ x: 1100 }}
          columns={[
            { title: '订单号', dataIndex: 'order_no', width: 200, ellipsis: true },
            {
              title: '目标',
              dataIndex: 'target_type',
              width: 80,
              render: (v: string) => (v === 'org' ? '组织' : '个人'),
            },
            { title: '用户', dataIndex: 'user_id', width: 140, ellipsis: true },
            { title: '组织', dataIndex: 'org_id', width: 140, ellipsis: true },
            {
              title: '金额',
              dataIndex: 'amount',
              width: 100,
              render: (v: number) => formatMoney(v),
            },
            { title: '渠道', dataIndex: 'provider', width: 80 },
            {
              title: '状态',
              dataIndex: 'status',
              width: 90,
              render: (v: string) => (
                <Tag color={STATUS_COLOR[v] || 'default'}>{v}</Tag>
              ),
            },
            {
              title: '支付时间',
              dataIndex: 'paid_at',
              width: 160,
              render: (v: string | null) => (v ? formatDateTime(v) : '—'),
            },
            {
              title: '创建时间',
              dataIndex: 'created_at',
              width: 160,
              render: (v: string) => formatDateTime(v),
            },
            {
              title: '失败原因',
              dataIndex: 'fail_reason',
              ellipsis: true,
            },
          ]}
        />
      </Card>
    </Space>
  )
}
