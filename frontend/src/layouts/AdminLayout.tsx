import { useState } from 'react'
import { App as AntApp, Layout, Menu, Typography, Button, Result, Space, Spin } from 'antd'
import {
  ApiOutlined,
  RobotOutlined,
  TeamOutlined,
  SafetyOutlined,
  WalletOutlined,
  BarChartOutlined,
  BankOutlined,
  PayCircleOutlined,
  LineChartOutlined,
  DatabaseOutlined,
  IdcardOutlined,
  AppstoreOutlined,
  LogoutOutlined,
} from '@ant-design/icons'
import type { ReactNode } from 'react'
import { Link, Navigate, Outlet, useLocation, useNavigate } from 'react-router-dom'
import { adminHomePath, useAdminAuth } from '../auth/AdminAuthContext'
import { Brand } from './Brand'

const { Header, Sider, Content } = Layout

// 菜单项本身由后端 /admin/auth/me 按权限下发（见 app/core/admin_permissions.py），
// 前端只负责把 icon 名映射成组件。
const ICONS: Record<string, ReactNode> = {
  channel: <ApiOutlined />,
  model: <RobotOutlined />,
  user: <TeamOutlined />,
  role: <SafetyOutlined />,
  kb: <DatabaseOutlined />,
  org: <BankOutlined />,
  wallet: <WalletOutlined />,
  payment: <PayCircleOutlined />,
  stats: <BarChartOutlined />,
  report: <LineChartOutlined />,
  admin_account: <IdcardOutlined />,
}

/** /admin 的落地页因人而异，取该管理员第一个有权访问的菜单 */
export function AdminHome() {
  const { admin } = useAdminAuth()
  return <Navigate to={adminHomePath(admin)} replace />
}

export function AdminLayout() {
  const { admin, loading, logout } = useAdminAuth()
  const location = useLocation()
  const navigate = useNavigate()
  const [collapsed, setCollapsed] = useState(false)

  if (loading) {
    return (
      <div className="th-loading">
        <Spin size="large" />
      </div>
    )
  }

  if (!admin) {
    return <Navigate to="/admin/login" replace state={{ from: location.pathname }} />
  }

  if (admin.menus.length === 0) {
    return (
      <div className="th-loading">
        <Result
          status="403"
          title="尚未分配任何后台权限"
          subTitle="请联系超级管理员为该账号分配后台角色。"
          extra={
            <Button
              onClick={() => {
                logout()
                navigate('/admin/login')
              }}
            >
              退出登录
            </Button>
          }
        />
      </div>
    )
  }

  // 手输 URL 也不能进无权访问的页面，否则整页只会刷出一串 403 提示
  const allowed = admin.menus.some(
    m => location.pathname === m.path || location.pathname.startsWith(`${m.path}/`),
  )
  if (location.pathname !== '/admin' && !allowed) {
    return <Navigate to={adminHomePath(admin)} replace />
  }

  const initial = admin.username.slice(0, 1).toUpperCase()
  const menuItems = admin.menus.map(m => ({
    key: m.path,
    icon: ICONS[m.icon] ?? <AppstoreOutlined />,
    label: <Link to={m.path}>{m.name}</Link>,
  }))

  return (
    <AntApp>
      <Layout style={{ minHeight: '100vh' }}>
        <Sider
          className="th-sider"
          breakpoint="lg"
          collapsedWidth={64}
          collapsed={collapsed}
          onCollapse={setCollapsed}
          theme="light"
          width={228}
        >
          <div className="th-sider-brand">
            <Brand subtitle="管理后台" compact={collapsed} />
          </div>
          <Menu mode="inline" selectedKeys={[location.pathname]} items={menuItems} />
        </Sider>
        <Layout>
          <Header className="th-header">
            <div className="th-userchip">
              <span className="th-avatar">{initial}</span>
              <Typography.Text ellipsis>{admin.username}</Typography.Text>
            </div>
            <Space>
              <Button
                type="text"
                icon={<LogoutOutlined />}
                onClick={() => {
                  logout()
                  navigate('/admin/login')
                }}
              >
                退出
              </Button>
            </Space>
          </Header>
          <Content className="th-content">
            <Outlet />
          </Content>
        </Layout>
      </Layout>
    </AntApp>
  )
}
