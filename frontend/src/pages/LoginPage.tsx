import { Button, Card, Form, Input, Typography, message } from 'antd'
import { Link, Navigate, useNavigate } from 'react-router-dom'
import { login, fetchMe } from '../api/auth'
import { ApiError } from '../api/client'
import { useAuth } from '../auth/AuthContext'
import { Brand } from '../layouts/Brand'
import { useState } from 'react'

export function LoginPage() {
  const { user, setSession, loading } = useAuth()
  const navigate = useNavigate()
  const [submitting, setSubmitting] = useState(false)

  if (!loading && user) {
    return <Navigate to="/chat" replace />
  }

  return (
    <div className="auth-shell">
      <div className="auth-panel">
        <Brand subtitle="用户控制台" />
        <Card className="auth-card">
          <Typography.Title level={3} style={{ marginTop: 0, marginBottom: 4 }}>
            登录
          </Typography.Title>
          <Typography.Paragraph type="secondary">
            使用邮箱与密码登录。模型调用仍需平台 API Key。
          </Typography.Paragraph>
          <Form
            layout="vertical"
            requiredMark={false}
            onFinish={async (values: { email: string; password: string }) => {
              setSubmitting(true)
              try {
                const tokenRes = await login(values.email, values.password)
                const me = await fetchMe(tokenRes.access_token)
                setSession(tokenRes.access_token, me)
                message.success('登录成功')
                navigate('/chat')
              } catch (err) {
                message.error(err instanceof ApiError ? err.message : '登录失败')
              } finally {
                setSubmitting(false)
              }
            }}
          >
            <Form.Item
              name="email"
              label="邮箱"
              rules={[{ required: true, type: 'email', message: '请输入有效邮箱' }]}
            >
              <Input size="large" placeholder="you@example.com" autoComplete="email" />
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
          <div className="auth-footer">
            还没有账号？<Link to="/register">注册</Link>
          </div>
        </Card>
      </div>
    </div>
  )
}
