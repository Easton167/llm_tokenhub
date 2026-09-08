import { useEffect, useState } from 'react'
import {
  Button,
  Card,
  Input,
  Modal,
  Select,
  Space,
  Table,
  Tag,
  Typography,
  message,
} from 'antd'
import { adminApi, type AdminUser, type Role, type UserRoleOut } from '../../api/admin'
import { ApiError } from '../../api/client'
import { useAdminAuth } from '../../auth/AdminAuthContext'
import { formatDateTime } from '../../utils/datetime'

function UserRolePanel({
  token,
  user,
  roles,
  onClose,
}: {
  token: string
  user: AdminUser | null
  roles: Role[]
  onClose: () => void
}) {
  const [userRoles, setUserRoles] = useState<UserRoleOut[]>([])
  const [loading, setLoading] = useState(false)
  const [selectedRole, setSelectedRole] = useState<string | undefined>()

  const load = async () => {
    if (!user) return
    setLoading(true)
    try {
      setUserRoles(await adminApi.listUserRoles(token, user.id))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { void load() }, [user?.id])

  const handleAssign = async () => {
    if (!selectedRole || !user) return
    try {
      await adminApi.assignRoleToUser(token, user.id, selectedRole)
      message.success('角色已分配')
      setSelectedRole(undefined)
      void load()
    } catch (e) {
      message.error(e instanceof ApiError ? e.message : '操作失败')
    }
  }

  const handleRemove = async (roleId: string) => {
    if (!user) return
    try {
      await adminApi.removeRoleFromUser(token, user.id, roleId)
      message.success('已移除')
      void load()
    } catch (e) {
      message.error(e instanceof ApiError ? e.message : '操作失败')
    }
  }

  const assignedIds = new Set(userRoles.map(r => r.role_id))
  const availableRoles = roles.filter(r => !assignedIds.has(r.id))

  if (!user) return null

  return (
    <Modal
      open={!!user}
      title={`角色分配 — ${user.nickname ?? user.email}`}
      onCancel={onClose}
      footer={null}
      width={520}
    >
      <Space.Compact style={{ width: '100%', marginBottom: 12 }}>
        <Select
          placeholder="选择角色"
          style={{ flex: 1 }}
          value={selectedRole}
          onChange={setSelectedRole}
          options={availableRoles.map(r => ({ value: r.id, label: r.name }))}
        />
        <Button type="primary" onClick={handleAssign} disabled={!selectedRole}>分配</Button>
      </Space.Compact>
      <Table
        rowKey="role_id"
        loading={loading}
        dataSource={userRoles}
        pagination={false}
        size="small"
        columns={[
          {
            title: '角色',
            dataIndex: 'role_id',
            render: (id: string) => roles.find(r => r.id === id)?.name ?? id.slice(0, 8),
          },
          {
            title: '分配时间',
            dataIndex: 'created_at',
            render: (v: string) => formatDateTime(v),
          },
          {
            title: '操作', width: 80,
            render: (_, row) => (
              <Button size="small" danger onClick={() => handleRemove(row.role_id)}>移除</Button>
            ),
          },
        ]}
      />
    </Modal>
  )
}

function PasswordPanel({
  token,
  user,
  onClose,
}: {
  token: string
  user: AdminUser | null
  onClose: () => void
}) {
  const [password, setPassword] = useState('')
  const [confirm, setConfirm] = useState('')
  const [saving, setSaving] = useState(false)

  useEffect(() => { setPassword(''); setConfirm('') }, [user?.id])

  if (!user) return null

  const error = password.length > 0 && password.length < 8
    ? '密码至少 8 位'
    : confirm.length > 0 && confirm !== password
      ? '两次输入不一致'
      : ''

  const handleSubmit = async () => {
    setSaving(true)
    try {
      await adminApi.resetUserPassword(token, user.id, password)
      message.success('密码已修改')
      onClose()
    } catch (e) {
      message.error(e instanceof ApiError ? e.message : '操作失败')
    } finally {
      setSaving(false)
    }
  }

  return (
    <Modal
      open
      title={`修改密码 — ${user.nickname ?? user.email}`}
      onCancel={onClose}
      onOk={handleSubmit}
      okText="保存"
      okButtonProps={{ disabled: !!error || password.length < 8 || confirm !== password, loading: saving }}
      width={420}
    >
      <Space direction="vertical" size="small" style={{ width: '100%' }}>
        <Input.Password
          placeholder="新密码（至少 8 位）"
          value={password}
          onChange={e => setPassword(e.target.value)}
        />
        <Input.Password
          placeholder="确认新密码"
          value={confirm}
          onChange={e => setConfirm(e.target.value)}
        />
        {error && <Typography.Text type="danger">{error}</Typography.Text>}
      </Space>
    </Modal>
  )
}

export function UsersPage() {
  const { token } = useAdminAuth()
  const [users, setUsers] = useState<AdminUser[]>([])
  const [roles, setRoles] = useState<Role[]>([])
  const [loading, setLoading] = useState(false)
  const [roleUser, setRoleUser] = useState<AdminUser | null>(null)
  const [pwdUser, setPwdUser] = useState<AdminUser | null>(null)

  if (!token) return null

  const load = async () => {
    setLoading(true)
    try {
      const [u, r] = await Promise.all([adminApi.listUsers(token), adminApi.listRoles(token)])
      setUsers(u)
      setRoles(r)
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
        <Typography.Title level={3} style={{ marginBottom: 4 }}>用户管理</Typography.Title>
        <Typography.Text type="secondary">查看用户列表，分配或移除角色。</Typography.Text>
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
              title: '状态', dataIndex: 'status', width: 80,
              render: (v: number) => v === 0
                ? <Tag color="green">正常</Tag>
                : <Tag color="red">禁用</Tag>,
            },
            { title: '计费主体', dataIndex: 'default_billing_subject', width: 100 },
            {
              title: '注册时间',
              dataIndex: 'created_at',
              width: 180,
              render: (v: string) => formatDateTime(v),
            },
            {
              title: '操作', width: 180,
              render: (_, row) => (
                <Space size="small">
                  <Button size="small" onClick={() => setRoleUser(row)}>角色管理</Button>
                  <Button size="small" onClick={() => setPwdUser(row)}>修改密码</Button>
                </Space>
              ),
            },
          ]}
        />
      </Card>
      <UserRolePanel
        token={token}
        user={roleUser}
        roles={roles}
        onClose={() => setRoleUser(null)}
      />
      <PasswordPanel token={token} user={pwdUser} onClose={() => setPwdUser(null)} />
    </Space>
  )
}
