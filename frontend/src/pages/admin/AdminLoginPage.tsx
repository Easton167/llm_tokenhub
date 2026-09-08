import { Alert, Button, Card, Form, Input, Typography, message } from 'antd'
import { Navigate, useNavigate } from 'react-router-dom'
import { useState } from 'react'
import { apiRequest, ApiError } from '../../api/client'
import { adminHomePath, useAdminAuth, type AdminUser } from '../../auth/AdminAuthContext'
import { Brand } from '../../layouts/Brand'

type TokenResponse = {
  access_token: string
  token_type: string
}

export function AdminLoginPage() {
  const { admin, loading, setSession } = useAdminAuth()
  const navigate = useNavigate()
  const [submitting, setSubmitting] = useState(false)

  if (!loading && admin) {
    return <Navigate to={adminHomePath(admin)} replace />
  }

  return (
    <div className="auth-shell">
      <div className="auth-panel">
        <Brand subtitle="管理后台" />
        <Card className="auth-card">
          <Typography.Title level={3} style={{ marginTop: 0, marginBottom: 4 }}>
            管理员登录
          </Typography.Title>
          <Typography.Paragraph type="secondary">
            使用后台管理员账号登录。与用户端注册账号相互独立。
          </Typography.Paragraph>
          <Form
            layout="vertical"
            requiredMark={false}
            initialValues={{ username: 'admin' }}
            onFinish={async (values: { username: string; password: string }) => {
              setSubmitting(true)
              try {
                const tokenRes = await apiRequest<TokenResponse>('/admin/auth/login', {
                  body: values,
                })
                const me = await apiRequest<AdminUser>('/admin/auth/me', {
                  token: tokenRes.access_token,
                })
                setSession(tokenRes.access_token, me)
                message.success('登录成功')
                navigate(adminHomePath(me))
              } catch (err) {
                message.error(err instanceof ApiError ? err.message : '登录失败')
              } finally {
                setSubmitting(false)
              }
            }}
          >
            <Form.Item
              name="username"
              label="用户名"
              rules={[{ required: true, message: '请输入用户名' }]}
            >
              <Input size="large" placeholder="admin" autoComplete="username" />
            </Form.Item>
            <Form.Item
              name="password"
              label="密码"
              rules={[{ required: true, message: '请输入密码' }]}
            >
              <Input.Password size="large" autoComplete="current-password" />
            </Form.Item>
            <Button type="primary" htmlType="submit" block size="large" loading={submitting}>
              登录
            </Button>
          </Form>
          <Alert
            style={{ marginTop: 20 }}
            type="info"
            showIcon
            message="尚未创建后台账号时，可用环境变量里的 ADMIN_USERNAME / ADMIN_PASSWORD 登录（默认 admin / admin123）"
          />
        </Card>
      </div>
    </div>
  )
}
