import { Button, Card, Form, Input, Typography, message } from 'antd'
import { Link, Navigate, useNavigate } from 'react-router-dom'
import { login, register, fetchMe } from '../api/auth'
import { ApiError } from '../api/client'
import { useAuth } from '../auth/AuthContext'
import { Brand } from '../layouts/Brand'
import { useState } from 'react'

export function RegisterPage() {
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
            注册
          </Typography.Title>
          <Typography.Paragraph type="secondary">
            注册后将自动创建个人钱包（充值余额与赠送余额均为 0）。
          </Typography.Paragraph>
          <Form
            layout="vertical"
            requiredMark={false}
            onFinish={async (values: {
              email: string
              password: string
              nickname?: string
            }) => {
              setSubmitting(true)
              try {
                await register(values.email, values.password, values.nickname)
                const tokenRes = await login(values.email, values.password)
                const me = await fetchMe(tokenRes.access_token)
                setSession(tokenRes.access_token, me)
                message.success('注册成功')
                navigate('/keys')
              } catch (err) {
                message.error(err instanceof ApiError ? err.message : '注册失败')
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
            <Form.Item name="nickname" label="昵称">
              <Input size="large" placeholder="可选" />
            </Form.Item>
            <Form.Item
              name="password"
              label="密码"
              rules={[
                { required: true, message: '请输入密码' },
                { min: 8, message: '至少 8 位' },
              ]}
            >
              <Input.Password size="large" autoComplete="new-password" />
            </Form.Item>
            <Button type="primary" htmlType="submit" block size="large" loading={submitting}>
              注册
            </Button>
          </Form>
          <div className="auth-footer">
            已有账号？<Link to="/login">登录</Link>
          </div>
        </Card>
      </div>
    </div>
  )
}
