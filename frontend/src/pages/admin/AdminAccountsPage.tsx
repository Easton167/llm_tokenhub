import { useEffect, useMemo, useState } from 'react'
import {
  Button,
  Card,
  Checkbox,
  Form,
  Input,
  Modal,
  Select,
  Space,
  Switch,
  Table,
  Tabs,
  Tag,
  Typography,
  message,
} from 'antd'
import { PlusOutlined, EditOutlined, DeleteOutlined, KeyOutlined, SafetyOutlined } from '@ant-design/icons'
import {
  adminApi,
  type AdminAccount,
  type AdminRoleDef,
  type PermissionGroup,
} from '../../api/admin'
import { ApiError } from '../../api/client'
import { useAdminAuth } from '../../auth/AdminAuthContext'
import { formatDateTime } from '../../utils/datetime'

const STATUS_MAP: Record<number, { text: string; color: string }> = {
  0: { text: '正常', color: 'green' },
  1: { text: '已禁用', color: 'red' },
}

function errText(e: unknown, fallback: string) {
  return e instanceof ApiError ? e.message : fallback
}

/** 按资源分组的权限勾选表；受控组件，值为权限码数组 */
function PermissionPicker({
  groups,
  value = [],
  onChange,
}: {
  groups: PermissionGroup[]
  value?: string[]
  onChange?: (next: string[]) => void
}) {
  const selected = new Set(value)

  const toggleGroup = (group: PermissionGroup, checked: boolean) => {
    const codes = group.permissions.map(p => p.code)
    const next = checked
      ? [...value, ...codes.filter(c => !selected.has(c))]
      : value.filter(c => !codes.includes(c))
    onChange?.(next)
  }

  return (
    <Table
      rowKey="resource"
      size="small"
      pagination={false}
      dataSource={groups}
      columns={[
        {
          title: '模块',
          dataIndex: 'name',
          width: 150,
          render: (name: string, group) => {
            const codes = group.permissions.map(p => p.code)
            const checkedCount = codes.filter(c => selected.has(c)).length
            return (
              <Checkbox
                checked={checkedCount === codes.length}
                indeterminate={checkedCount > 0 && checkedCount < codes.length}
                onChange={e => toggleGroup(group, e.target.checked)}
              >
                {name}
              </Checkbox>
            )
          },
        },
        {
          title: '操作权限',
          render: (_, group) => (
            <Checkbox.Group
              value={group.permissions.map(p => p.code).filter(c => selected.has(c))}
              options={group.permissions.map(p => ({ label: p.name, value: p.code }))}
              onChange={next => {
                const codes = group.permissions.map(p => p.code)
                onChange?.([...value.filter(c => !codes.includes(c)), ...(next as string[])])
              }}
            />
          ),
        },
      ]}
    />
  )
}

function RolesTab({ token, groups }: { token: string; groups: PermissionGroup[] }) {
  const [roles, setRoles] = useState<AdminRoleDef[]>([])
  const [loading, setLoading] = useState(false)
  const [modalOpen, setModalOpen] = useState(false)
  const [editing, setEditing] = useState<AdminRoleDef | null>(null)
  const [form] = Form.useForm()

  const load = async () => {
    setLoading(true)
    try {
      setRoles(await adminApi.listAdminRoles(token))
    } catch (e) {
      message.error(errText(e, '加载失败'))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { void load() }, [token])

  const openCreate = () => {
    setEditing(null)
    form.resetFields()
    form.setFieldsValue({ data_scope: 'all', permissions: [] })
    setModalOpen(true)
  }

  const openEdit = (role: AdminRoleDef) => {
    setEditing(role)
    form.setFieldsValue(role)
    setModalOpen(true)
  }

  const handleOk = async () => {
    const values = await form.validateFields()
    try {
      if (editing) {
        const { code: _code, ...patch } = values
        await adminApi.updateAdminRole(token, editing.id, patch)
        message.success('已更新')
      } else {
        await adminApi.createAdminRole(token, values)
        message.success('已创建')
      }
      setModalOpen(false)
      void load()
    } catch (e) {
      message.error(errText(e, '操作失败'))
    }
  }

  const handleDelete = (role: AdminRoleDef) => {
    Modal.confirm({
      title: `删除后台角色「${role.name}」？`,
      content: '仍被账号引用的角色无法删除。',
      onOk: async () => {
        try {
          await adminApi.deleteAdminRole(token, role.id)
          message.success('已删除')
          void load()
        } catch (e) {
          message.error(errText(e, '删除失败'))
        }
      },
    })
  }

  return (
    <>
      <div style={{ display: 'flex', justifyContent: 'flex-end', marginBottom: 12 }}>
        <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>新建后台角色</Button>
      </div>
      <Table
        rowKey="id"
        loading={loading}
        dataSource={roles}
        pagination={false}
        scroll={{ x: 900 }}
        columns={[
          { title: '标识', dataIndex: 'code', width: 140 },
          { title: '名称', dataIndex: 'name', width: 140 },
          {
            title: '权限',
            render: (_, row) =>
              row.permissions.length === 0
                ? <Typography.Text type="secondary">未授权</Typography.Text>
                : <Space size={[4, 4]} wrap>{row.permissions.map(c => <Tag key={c}>{c}</Tag>)}</Space>,
          },
          {
            title: '创建时间',
            dataIndex: 'created_at',
            width: 170,
            render: (v: string) => formatDateTime(v),
          },
          {
            title: '操作', width: 110, fixed: 'right',
            render: (_, row) => (
              <Space>
                <Button size="small" icon={<EditOutlined />} onClick={() => openEdit(row)} />
                <Button size="small" danger icon={<DeleteOutlined />} onClick={() => handleDelete(row)} />
              </Space>
            ),
          },
        ]}
      />

      <Modal
        open={modalOpen}
        title={editing ? `编辑角色 — ${editing.name}` : '新建后台角色'}
        onOk={handleOk}
        onCancel={() => setModalOpen(false)}
        destroyOnHidden
        width={760}
      >
        <Form form={form} layout="vertical" style={{ marginTop: 8 }}>
          <Space size="large" style={{ display: 'flex' }}>
            <Form.Item
              name="code"
              label="标识"
              rules={[
                { required: true, message: '请输入标识' },
                { pattern: /^[a-z0-9_-]+$/, message: '只能用小写字母、数字、下划线与连字符' },
              ]}
              style={{ flex: 1 }}
            >
              <Input placeholder="如 ops" disabled={!!editing} />
            </Form.Item>
            <Form.Item name="name" label="名称" rules={[{ required: true }]} style={{ flex: 1 }}>
              <Input placeholder="如 运营" />
            </Form.Item>
          </Space>
          <Form.Item name="description" label="描述">
            <Input.TextArea rows={2} />
          </Form.Item>
          <Form.Item
            name="data_scope"
            label="数据权限"
            extra="已建模但当前尚未生效：后台查询仍是全量可见。"
          >
            <Select
              options={[
                { value: 'all', label: '全部数据' },
                { value: 'self', label: '仅本人创建的数据' },
              ]}
            />
          </Form.Item>
          <Form.Item name="permissions" label="菜单与操作权限">
            <PermissionPicker groups={groups} />
          </Form.Item>
        </Form>
      </Modal>
    </>
  )
}

function AccountsTab({ token, roles }: { token: string; roles: AdminRoleDef[] }) {
  const { admin } = useAdminAuth()
  const [accounts, setAccounts] = useState<AdminAccount[]>([])
  const [loading, setLoading] = useState(false)
  const [modalOpen, setModalOpen] = useState(false)
  const [editing, setEditing] = useState<AdminAccount | null>(null)
  const [pwdTarget, setPwdTarget] = useState<AdminAccount | null>(null)
  const [form] = Form.useForm()
  const [pwdForm] = Form.useForm()

  const roleOptions = roles.map(r => ({ value: r.id, label: `${r.name}（${r.code}）` }))

  const load = async () => {
    setLoading(true)
    try {
      setAccounts(await adminApi.listAdminAccounts(token))
    } catch (e) {
      message.error(errText(e, '加载失败'))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { void load() }, [token])

  const openCreate = () => {
    setEditing(null)
    form.resetFields()
    form.setFieldsValue({ is_superadmin: false, role_ids: [] })
    setModalOpen(true)
  }

  const openEdit = (account: AdminAccount) => {
    setEditing(account)
    form.setFieldsValue(account)
    setModalOpen(true)
  }

  const handleOk = async () => {
    const values = await form.validateFields()
    try {
      if (editing) {
        const { username: _u, password: _p, ...patch } = values
        await adminApi.updateAdminAccount(token, editing.id, patch)
        message.success('已更新')
      } else {
        await adminApi.createAdminAccount(token, values)
        message.success('已创建')
      }
      setModalOpen(false)
      void load()
    } catch (e) {
      message.error(errText(e, '操作失败'))
    }
  }

  const handleResetPassword = async () => {
    if (!pwdTarget) return
    const { password } = await pwdForm.validateFields()
    try {
      await adminApi.resetAdminAccountPassword(token, pwdTarget.id, password)
      message.success('密码已重置')
      setPwdTarget(null)
      pwdForm.resetFields()
    } catch (e) {
      message.error(errText(e, '重置失败'))
    }
  }

  const handleDelete = (account: AdminAccount) => {
    Modal.confirm({
      title: `删除后台账号「${account.username}」？`,
      content: '删除后该账号立即无法登录，操作不可撤销。',
      okButtonProps: { danger: true },
      onOk: async () => {
        try {
          await adminApi.deleteAdminAccount(token, account.id)
          message.success('已删除')
          void load()
        } catch (e) {
          message.error(errText(e, '删除失败'))
        }
      },
    })
  }

  const toggleStatus = async (account: AdminAccount) => {
    try {
      await adminApi.updateAdminAccount(token, account.id, {
        status: account.status === 0 ? 1 : 0,
      })
      void load()
    } catch (e) {
      message.error(errText(e, '操作失败'))
    }
  }

  return (
    <>
      <div style={{ display: 'flex', justifyContent: 'flex-end', marginBottom: 12 }}>
        <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>新建后台账号</Button>
      </div>
      <Table
        rowKey="id"
        loading={loading}
        dataSource={accounts}
        pagination={false}
        scroll={{ x: 900 }}
        columns={[
          {
            title: '用户名', dataIndex: 'username', width: 160,
            render: (v: string, row) => (
              <Space>
                {v}
                {row.is_superadmin && <Tag color="gold">超管</Tag>}
                {v === admin?.username && <Tag>当前登录</Tag>}
              </Space>
            ),
          },
          {
            title: '显示名', dataIndex: 'display_name',
            width: 140,
            render: (v: string | null) => v ?? '—',
          },
          {
            title: '后台角色',
            render: (_, row) =>
              row.is_superadmin
                ? <Typography.Text type="secondary">超管拥有全部权限</Typography.Text>
                : row.role_ids.length === 0
                  ? <Typography.Text type="secondary">未分配</Typography.Text>
                  : (
                    <Space size={[4, 4]} wrap>
                      {row.role_ids.map(id => (
                        <Tag key={id}>{roles.find(r => r.id === id)?.name ?? id}</Tag>
                      ))}
                    </Space>
                  ),
          },
          {
            title: '状态', dataIndex: 'status', width: 90,
            render: (v: number) => <Tag color={STATUS_MAP[v]?.color}>{STATUS_MAP[v]?.text ?? v}</Tag>,
          },
          {
            title: '创建时间', dataIndex: 'created_at', width: 170,
            render: (v: string) => formatDateTime(v),
          },
          {
            title: '操作', width: 190, fixed: 'right',
            render: (_, row) => (
              <Space>
                <Button size="small" icon={<EditOutlined />} onClick={() => openEdit(row)} />
                <Button size="small" icon={<KeyOutlined />} onClick={() => setPwdTarget(row)} />
                <Button
                  size="small"
                  disabled={row.username === admin?.username}
                  onClick={() => toggleStatus(row)}
                >
                  {row.status === 0 ? '禁用' : '启用'}
                </Button>
                <Button
                  size="small"
                  danger
                  icon={<DeleteOutlined />}
                  disabled={row.username === admin?.username}
                  onClick={() => handleDelete(row)}
                />
              </Space>
            ),
          },
        ]}
      />

      <Modal
        open={modalOpen}
        title={editing ? `编辑账号 — ${editing.username}` : '新建后台账号'}
        onOk={handleOk}
        onCancel={() => setModalOpen(false)}
        destroyOnHidden
        width={520}
      >
        <Form form={form} layout="vertical" style={{ marginTop: 8 }}>
          {!editing && (
            <>
              <Form.Item
                name="username"
                label="用户名"
                rules={[
                  { required: true, message: '请输入用户名' },
                  { pattern: /^[A-Za-z0-9_.-]+$/, message: '只能用字母、数字、下划线、点与连字符' },
                ]}
              >
                <Input autoComplete="off" />
              </Form.Item>
              <Form.Item
                name="password"
                label="初始密码"
                rules={[{ required: true, min: 8, message: '至少 8 位' }]}
              >
                <Input.Password autoComplete="new-password" />
              </Form.Item>
            </>
          )}
          <Form.Item name="display_name" label="显示名">
            <Input />
          </Form.Item>
          <Form.Item
            name="is_superadmin"
            label="超级管理员"
            valuePropName="checked"
            extra="超管跳过全部权限校验，且只有超管能设置该开关。"
          >
            <Switch disabled={!admin?.is_superadmin} />
          </Form.Item>
          <Form.Item name="role_ids" label="后台角色">
            <Select mode="multiple" allowClear options={roleOptions} placeholder="可多选" />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        open={!!pwdTarget}
        title={`重置密码 — ${pwdTarget?.username ?? ''}`}
        onOk={handleResetPassword}
        onCancel={() => { setPwdTarget(null); pwdForm.resetFields() }}
        destroyOnHidden
        width={420}
      >
        <Form form={pwdForm} layout="vertical" style={{ marginTop: 8 }}>
          <Form.Item
            name="password"
            label="新密码"
            rules={[{ required: true, min: 8, message: '至少 8 位' }]}
          >
            <Input.Password autoComplete="new-password" />
          </Form.Item>
        </Form>
      </Modal>
    </>
  )
}

export function AdminAccountsPage() {
  const { token } = useAdminAuth()
  const [groups, setGroups] = useState<PermissionGroup[]>([])
  const [roles, setRoles] = useState<AdminRoleDef[]>([])
  const [activeKey, setActiveKey] = useState('accounts')

  useEffect(() => {
    if (!token) return
    void (async () => {
      try {
        const [g, r] = await Promise.all([
          adminApi.listPermissionGroups(token),
          adminApi.listAdminRoles(token),
        ])
        setGroups(g)
        setRoles(r)
      } catch (e) {
        message.error(errText(e, '加载失败'))
      }
    })()
  }, [token, activeKey])

  const tabs = useMemo(
    () => [
      {
        key: 'accounts',
        label: '后台账号',
        children: token ? <AccountsTab token={token} roles={roles} /> : null,
      },
      {
        key: 'roles',
        label: (
          <span>
            <SafetyOutlined /> 后台角色
          </span>
        ),
        children: token ? <RolesTab token={token} groups={groups} /> : null,
      },
    ],
    [token, roles, groups],
  )

  if (!token) return null

  return (
    <Space direction="vertical" size="large" style={{ width: '100%' }}>
      <div>
        <Typography.Title level={3} style={{ marginBottom: 4 }}>后台账号</Typography.Title>
        <Typography.Text type="secondary">
          管理后台管理员及其角色。后台角色决定可见菜单与可执行操作，与用户端的「角色权限」互不影响。
        </Typography.Text>
      </div>
      <Card>
        <Tabs activeKey={activeKey} onChange={setActiveKey} items={tabs} />
      </Card>
    </Space>
  )
}
