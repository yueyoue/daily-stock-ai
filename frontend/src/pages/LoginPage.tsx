import { Button, Card, Form, Input, message } from 'antd'
import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { authApi } from '../api/client'

export default function LoginPage() {
  const navigate = useNavigate()
  const [loading, setLoading] = useState(false)

  const onFinish = async (values: { username: string; password: string }) => {
    setLoading(true)
    try {
      await authApi.login(values.username, values.password)
      navigate('/', { replace: true })
    } catch (e) {
      message.error(e instanceof Error ? e.message : '登录失败')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div style={{ minHeight: '100vh', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
      <Card title="DailyStockAI · 登录后进入投研工作台" style={{ width: 380 }}>
        <Form onFinish={onFinish} initialValues={{ username: 'admin' }}>
          <Form.Item name="username" rules={[{ required: true, message: '请输入账号' }]}>
            <Input placeholder="账号" />
          </Form.Item>
          <Form.Item name="password" rules={[{ required: true, message: '请输入密码' }]}>
            <Input.Password placeholder="密码" />
          </Form.Item>
          <Button type="primary" htmlType="submit" block loading={loading}>
            登录
          </Button>
        </Form>
      </Card>
    </div>
  )
}
