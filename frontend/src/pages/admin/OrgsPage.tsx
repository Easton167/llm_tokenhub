import { useEffect, useState } from 'react'
import {
  Button,
  Card,
  Descriptions,
  Form,
  Input,
  Modal,
  Select,
  Space,
  Table,
  Tag,
  Typography,
  message,
} from 'antd'
import { ReloadOutlined } from '@ant-design/icons'
import {
  adminApi,
  type AdminOrg,
  type AdminOrgDetail,
} from '../../api/admin'
import { ApiError } from '../../api/client'
import { useAdminAuth } from '../../auth/AdminAuthContext'
import { formatDateTime } from '../../utils/datetime'
import { formatMoney } from '../../utils/money'

export function AdminOrgsPage() {
  const { token } = useAdminAuth()
  const [items, setItems] = useState<AdminOrg[]>([])
  const [loading, setLoading] = useState(false)
  const [statusFilter, setStatusFilter] = useState<number | undefined>(undefined)
  const [detail, setDetail] = useState<AdminOrgDetail | null>(null)
  const [deactivateOpen, setDeactivateOpen] = useState(false)
  const [target, setTarget] = useState<AdminOrg | null>(null)
  const [form] = Form.useForm()

  const load = async () => {
    if (!token) return
    setLoading(true)
    try {
      setItems(await adminApi.listOrgs(token, { status: statusFilter, limit: 100 }))
    } catch (err) {
      message.error(err instanceof ApiError ? err.message : '加载失败')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    void load()
  }, [token, statusFilter])

  return (
    <Space direction="vertical" size="large" style={{ width: '100%' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between' }}>
        <div>
          <Typography.Title level={3} style={{ margin: 0 }}>
            组织管理
          </Typography.Title>
          <Typography.Paragraph type="secondary" style={{ marginBottom: 0 }}>
            查看全部组织、成员与额度；可停用违规组织。
          </Typography.Paragraph>
        </div>
        <Space>
          <Select
            allowClear
            placeholder="状态"
            style={{ width: 120 }}
            value={statusFilter}
            onChange={setStatusFilter}
            options={[
              { value: 0, label: '活跃' },
              { value: 1, label: '停用' },
            ]}
          />
          <Button icon={<ReloadOutlined />} onClick={() => void load()} loading={loading}>
            刷新
          </Button>
        </Space>
      </div>

      <Card>
        <Table
          rowKey="id"
          loading={loading}
          dataSource={items}
          pagination={{ pageSize: 20 }}
          columns={[
            { title: '名称', dataIndex: 'name' },
            { title: 'Owner', dataIndex: 'owner_user_id', ellipsis: true },
            { title: '余额兜底', dataIndex: 'billing_fallback', width: 110 },
            {
              title: '状态',
              dataIndex: 'status',
              width: 80,
              render: (v: number) =>
                v === 0 ? <Tag color="green">活跃</Tag> : <Tag>停用</Tag>,
            },
            {
              title: '创建时间',
              dataIndex: 'created_at',
              render: (v: string) => formatDateTime(v),
            },
            {
              title: '操作',
              width: 180,
              render: (_, row) => (
                <Space>
                  <Button
                    size="small"
                    onClick={async () => {
                      if (!token) return
                      try {
                        setDetail(await adminApi.getOrg(token, row.id))
                      } catch (err) {
                        message.error(err instanceof ApiError ? err.message : '加载详情失败')
                      }
                    }}
                  >
                    详情
                  </Button>
                  {row.status === 0 && (
                    <Button
                      size="small"
                      danger
                      onClick={() => {
                        setTarget(row)
                        setDeactivateOpen(true)
                      }}
                    >
                      停用
                    </Button>
                  )}
                </Space>
              ),
            },
          ]}
        />
      </Card>

      <Modal
        open={!!detail}
        title={`组织详情：${detail?.name || ''}`}
        width={720}
        footer={null}
        onCancel={() => setDetail(null)}
      >
        {detail && (
          <>
            <Descriptions column={1} size="small" style={{ marginBottom: 16 }}>
              <Descriptions.Item label="ID">{detail.id}</Descriptions.Item>
              <Descriptions.Item label="Owner">{detail.owner_user_id}</Descriptions.Item>
              <Descriptions.Item label="billing_fallback">
                {detail.billing_fallback}
              </Descriptions.Item>
            </Descriptions>
            <Table
              rowKey="id"
              size="small"
              pagination={false}
              dataSource={detail.members}
              columns={[
                { title: '用户', dataIndex: 'user_id', ellipsis: true },
                { title: '角色', dataIndex: 'org_role', width: 90 },
                {
                  title: '消费组织钱包',
                  dataIndex: 'can_consume_org_wallet',
                  width: 120,
                  render: (v: boolean) => (v ? '是' : '否'),
                },
                {
                  title: '额度',
                  render: (_, m) => {
                    const q = m.quota
                    if (!q || q.quota_period === 'none') return '不限'
                    return `${q.quota_period} ${formatMoney(q.quota_used)}/${formatMoney(q.quota_limit)}`
                  },
                },
              ]}
            />
          </>
        )}
      </Modal>

      <Modal
        open={deactivateOpen}
        title="停用组织"
        okText="确认停用"
        okButtonProps={{ danger: true }}
        onCancel={() => {
          setDeactivateOpen(false)
          setTarget(null)
          form.resetFields()
        }}
        onOk={async () => {
          if (!token || !target) return
          const values = await form.validateFields()
          try {
            await adminApi.deactivateOrg(token, target.id, values.reason)
            message.success('已停用')
            setDeactivateOpen(false)
            setTarget(null)
            form.resetFields()
            await load()
          } catch (err) {
            message.error(err instanceof ApiError ? err.message : '停用失败')
            throw err
          }
        }}
      >
        <Form form={form} layout="vertical">
          <Form.Item name="reason" label="原因（可选）">
            <Input.TextArea rows={3} placeholder="审计备注" />
          </Form.Item>
        </Form>
      </Modal>
    </Space>
  )
}
