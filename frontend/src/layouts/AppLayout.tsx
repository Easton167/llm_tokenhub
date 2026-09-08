import { useState } from 'react'
import { App as AntApp, Layout, Menu, Typography, Button, Space, Tag, Spin } from 'antd'
import {
  CommentOutlined,
  KeyOutlined,
  WalletOutlined,
  BarChartOutlined,
  BankOutlined,
  PayCircleOutlined,
  LineChartOutlined,
  DatabaseOutlined,
  LogoutOutlined,
} from '@ant-design/icons'
import { Link, Navigate, Outlet, useLocation, useNavigate } from 'react-router-dom'
import { useAuth } from '../auth/AuthContext'
import { Brand } from './Brand'

const { Header, Sider, Content } = Layout

const menuItems = [
  { key: '/chat', icon: <CommentOutlined />, label: <Link to="/chat">对话</Link> },
  { key: '/kb', icon: <DatabaseOutlined />, label: <Link to="/kb">知识库</Link> },
  { key: '/keys', icon: <KeyOutlined />, label: <Link to="/keys">API Key</Link> },
  { key: '/orgs', icon: <BankOutlined />, label: <Link to="/orgs">我的组织</Link> },
  { key: '/wallet', icon: <WalletOutlined />, label: <Link to="/wallet">钱包</Link> },
  { key: '/recharge', icon: <PayCircleOutlined />, label: <Link to="/recharge">在线充值</Link> },
  { key: '/usage', icon: <BarChartOutlined />, label: <Link to="/usage">用量明细</Link> },
  { key: '/reports', icon: <LineChartOutlined />, label: <Link to="/reports">用量报表</Link> },
]

function resolveSelectedKey(pathname: string): string {
  const matched = menuItems
    .map((item) => item.key)
    .filter((key) => pathname === key || pathname.startsWith(`${key}/`))
    .sort((a, b) => b.length - a.length)
  return matched[0] ?? pathname
}

export function AppLayout() {
  const { user, loading, logout, apiKey } = useAuth()
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

  if (!user) {
    return <Navigate to="/login" replace state={{ from: location.pathname }} />
  }

  const displayName = user.nickname || user.email
  const initial = displayName.slice(0, 1).toUpperCase()

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
            <Brand subtitle="用户控制台" compact={collapsed} />
          </div>
          <Menu
            mode="inline"
            selectedKeys={[resolveSelectedKey(location.pathname)]}
            items={menuItems}
          />
        </Sider>
        <Layout>
          <Header className="th-header">
            <div className="th-userchip">
              <span className="th-avatar">{initial}</span>
              <Typography.Text ellipsis>{displayName}</Typography.Text>
              {apiKey ? (
                <Tag color="success" bordered={false}>
                  Chat Key 已配置
                </Tag>
              ) : (
                <Tag color="warning" bordered={false}>
                  未配置 Chat Key
                </Tag>
              )}
            </div>
            <Space>
              <Button
                type="text"
                icon={<LogoutOutlined />}
                onClick={() => {
                  logout()
                  navigate('/login')
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
