import { useEffect, useState } from 'react'
import {
  Button,
  Card,
  Checkbox,
  Form,
  Input,
  InputNumber,
  Modal,
  Space,
  Table,
  Typography,
  message,
} from 'antd'
import { PlusOutlined, EditOutlined, DeleteOutlined, SafetyOutlined } from '@ant-design/icons'
import { adminApi, type LLMModel, type Role, type RoleModelGrant } from '../../api/admin'
import { ApiError } from '../../api/client'
import { useAdminAuth } from '../../auth/AdminAuthContext'
import { formatDateTime } from '../../utils/datetime'

function ModelGrantPanel({
  token,
  role,
  models,
  onClose,
}: {
  token: string
  role: Role | null
  models: LLMModel[]
  onClose: () => void
}) {
  const [grants, setGrants] = useState<RoleModelGrant[]>([])
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)

  const load = async () => {
    if (!role) return
    setLoading(true)
    try {
      setGrants(await adminApi.listRoleModelGrants(token, role.id))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { void load() }, [role?.id])

  const grantedIds = new Set(grants.map(g => g.model_id))

  const handleToggle = async (modelId: string, checked: boolean) => {
    if (!role) return
    setSaving(true)
    try {
      if (checked) {
        await adminApi.grantModelToRole(token, role.id, modelId)
      } else {
        await adminApi.revokeModelFromRole(token, role.id, modelId)
      }
      void load()
    } catch (e) {
      message.error(e instanceof ApiError ? e.message : '操作失败')
    } finally {
      setSaving(false)
    }
  }

  if (!role) return null

  return (
    <Modal
      open={!!role}
      title={`模型授权矩阵 — ${role.name}`}
      onCancel={onClose}
      footer={null}
      width={520}
    >
      <Typography.Text type="secondary" style={{ display: 'block', marginBottom: 12 }}>
        勾选后该角色的用户即可调用对应模型。
      </Typography.Text>
      <Table
        rowKey="id"
        loading={loading || saving}
        dataSource={models}
        pagination={false}
        size="small"
        columns={[
          { title: '模型别名', dataIndex: 'alias' },
          { title: '显示名', dataIndex: 'display_name' },
          {
            title: '已授权', width: 80,
            render: (_, m) => (
              <Checkbox
                checked={grantedIds.has(m.id)}
                onChange={e => handleToggle(m.id, e.target.checked)}
              />
            ),
          },
        ]}
      />
    </Modal>
  )
}

function formatBytes(v: number | null | undefined) {
  if (v == null) return '系统默认'
  if (v >= 1024 ** 3) return `${(v / 1024 ** 3).toFixed(1)} GB`
  if (v >= 1024 ** 2) return `${(v / 1024 ** 2).toFixed(0)} MB`
  if (v >= 1024) return `${(v / 1024).toFixed(0)} KB`
  return `${v} B`
}

export function RolesPage() {
  const { token } = useAdminAuth()
  const [roles, setRoles] = useState<Role[]>([])
  const [models, setModels] = useState<LLMModel[]>([])
  const [loading, setLoading] = useState(false)
  const [modalOpen, setModalOpen] = useState(false)
  const [editing, setEditing] = useState<Role | null>(null)
  const [grantRole, setGrantRole] = useState<Role | null>(null)
  const [form] = Form.useForm()

  if (!token) return null

  const load = async () => {
    setLoading(true)
    try {
      const [r, m] = await Promise.all([adminApi.listRoles(token), adminApi.listModels(token)])
      setRoles(r)
      setModels(m)
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

  const openEdit = (r: Role) => {
    setEditing(r)
    form.setFieldsValue(r)
    setModalOpen(true)
  }

  const handleOk = async () => {
    const values = await form.validateFields()
    try {
      if (editing) {
        await adminApi.updateRole(token, editing.id, values)
        message.success('更新成功')
      } else {
        await adminApi.createRole(token, values)
        message.success('创建成功')
      }
      setModalOpen(false)
      void load()
    } catch (e) {
      message.error(e instanceof ApiError ? e.message : '操作失败')
    }
  }

  const handleDelete = (r: Role) => {
    Modal.confirm({
      title: `删除角色「${r.name}」？`,
      content: '已分配给用户的角色无法删除。',
      onOk: async () => {
        try {
          await adminApi.deleteRole(token, r.id)
          message.success('已删除')
          void load()
        } catch (e) {
          message.error(e instanceof ApiError ? e.message : '删除失败')
        }
      },
    })
  }

  return (
    <Space direction="vertical" size="large" style={{ width: '100%' }}>
      <div>
        <Typography.Title level={3} style={{ marginBottom: 4 }}>角色权限</Typography.Title>
        <Typography.Text type="secondary">管理角色、模型授权与知识库配额。</Typography.Text>
      </div>
      <Card>
        <div style={{ display: 'flex', justifyContent: 'flex-end', marginBottom: 12 }}>
          <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>新建角色</Button>
        </div>
        <Table
          rowKey="id"
          loading={loading}
          dataSource={roles}
          pagination={false}
          scroll={{ x: 960 }}
          columns={[
            { title: '角色名', dataIndex: 'name', width: 140 },
            { title: '描述', dataIndex: 'description', render: (v: string | null) => v ?? '—' },
            {
              title: '库数上限', width: 100,
              dataIndex: 'kb_max_kb_count',
              render: (v: number | null) => v ?? '默认',
            },
            {
              title: '单库文档', width: 100,
              dataIndex: 'kb_max_docs_per_kb',
              render: (v: number | null) => v ?? '默认',
            },
            {
              title: '存储上限', width: 110,
              dataIndex: 'kb_max_storage_bytes',
              render: (v: number | null) => formatBytes(v),
            },
            {
              title: '单文件', width: 100,
              dataIndex: 'kb_max_file_size_bytes',
              render: (v: number | null) => formatBytes(v),
            },
            {
              title: '创建时间',
              dataIndex: 'created_at',
              width: 170,
              render: (v: string) => formatDateTime(v),
            },
            {
              title: '操作', width: 160, fixed: 'right',
              render: (_, row) => (
                <Space>
                  <Button size="small" icon={<SafetyOutlined />} onClick={() => setGrantRole(row)}>模型授权</Button>
                  <Button size="small" icon={<EditOutlined />} onClick={() => openEdit(row)} />
                  <Button size="small" danger icon={<DeleteOutlined />} onClick={() => handleDelete(row)} />
                </Space>
              ),
            },
          ]}
        />
      </Card>

      <Modal
        open={modalOpen}
        title={editing ? '编辑角色' : '新建角色'}
        onOk={handleOk}
        onCancel={() => setModalOpen(false)}
        destroyOnHidden
        width={520}
      >
        <Form form={form} layout="vertical" style={{ marginTop: 8 }}>
          <Form.Item name="name" label="角色名" rules={[{ required: true }]}>
            <Input placeholder="如 basic-user" />
          </Form.Item>
          <Form.Item name="description" label="描述">
            <Input.TextArea rows={2} />
          </Form.Item>
          <Typography.Text type="secondary" style={{ display: 'block', marginBottom: 8 }}>
            知识库配额（留空表示使用系统默认值）
          </Typography.Text>
          <Form.Item name="kb_max_kb_count" label="最大知识库数 / 用户">
            <InputNumber min={0} style={{ width: '100%' }} placeholder="系统默认" />
          </Form.Item>
          <Form.Item name="kb_max_docs_per_kb" label="单库最大文档数">
            <InputNumber min={0} style={{ width: '100%' }} placeholder="系统默认" />
          </Form.Item>
          <Form.Item name="kb_max_storage_bytes" label="存储上限（字节）">
            <InputNumber min={0} style={{ width: '100%' }} placeholder="系统默认，如 1073741824=1GB" />
          </Form.Item>
          <Form.Item name="kb_max_file_size_bytes" label="单文件上限（字节）">
            <InputNumber min={0} style={{ width: '100%' }} placeholder="系统默认，如 20971520=20MB" />
          </Form.Item>
        </Form>
      </Modal>

      <ModelGrantPanel
        token={token}
        role={grantRole}
        models={models}
        onClose={() => setGrantRole(null)}
      />
    </Space>
  )
}
