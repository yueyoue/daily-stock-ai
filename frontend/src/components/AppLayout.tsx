import { Button, Layout, Menu, Space, Tag, Typography, message } from 'antd'
import { useEffect, useState } from 'react'
import { Link, NavLink, Outlet, useNavigate } from 'react-router-dom'
import { authApi, collectApi, configApi, taskApi } from '../api/client'
import { useCurrentUser } from './RequireAuth'

const { Sider, Header, Content } = Layout

// 侧边栏结构对齐 docs/AI投研工作台.html 原型
const MENU = [
  { key: 'grp-work', label: '投研工作台', type: 'group' as const },
  { key: '/', label: <NavLink to="/">大盘仪表盘</NavLink> },
  { key: '/morning', label: <NavLink to="/morning">盘前早报</NavLink> },
  { key: '/afternoon', label: <NavLink to="/afternoon">盘后复盘</NavLink> },
  { key: '/selector', label: <NavLink to="/selector">选股器</NavLink> },
  { key: '/backtest', label: <NavLink to="/backtest">策略回测</NavLink> },
  { key: 'grp-mat', label: '素材库', type: 'group' as const },
  { key: '/hot', label: <NavLink to="/hot">热点题材库</NavLink> },
  { key: '/watch', label: <NavLink to="/watch">掘金观察池</NavLink> },
  { key: '/news', label: <NavLink to="/news">资讯中心</NavLink> },
  { key: 'grp-sys', label: '系统', type: 'group' as const },
  { key: '/task', label: <NavLink to="/task">任务监控</NavLink> },
  { key: '/config', label: <NavLink to="/config">系统配置</NavLink> },
]

/**
 * 工作台布局（T4.2，对齐原型顶栏）：
 * 交易日 / 模型 / 采集任务状态 / 手动触发采集 / 侧栏折叠 / 退出
 */
export default function AppLayout() {
  const navigate = useNavigate()
  const user = useCurrentUser()
  const [collapsed, setCollapsed] = useState(false)
  const [model, setModel] = useState<string>('—')
  const [collecting, setCollecting] = useState(false)
  const [lastTask, setLastTask] = useState<string>('空闲')

  useEffect(() => {
    configApi
      .get()
      .then((c) => setModel(c.llm_model))
      .catch(() => setModel('未知'))
    taskApi.latest(1).then((p) => {
      const t = p.items[0]
      if (t) setLastTask(t.status === 'running' ? '运行中' : '空闲')
    }).catch(() => undefined)
  }, [])

  const triggerCollect = async () => {
    setCollecting(true)
    setLastTask('运行中')
    try {
      const r = await collectApi.trigger('incremental')
      message.success(`采集任务已触发（task_id=${r.task_id}）`)
    } catch (e) {
      message.error(e instanceof Error ? e.message : '触发失败')
      setLastTask('空闲')
    } finally {
      setCollecting(false)
    }
  }

  const logout = async () => {
    try {
      await authApi.logout()
    } finally {
      navigate('/login', { replace: true })
    }
  }

  return (
    <Layout style={{ minHeight: '100vh' }}>
      <Sider
        theme="dark"
        width={220}
        collapsedWidth={64}
        collapsed={collapsed}
        trigger={null}
        collapsible
      >
        <div style={{ padding: 16, color: '#e2e8f0', fontWeight: 700, whiteSpace: 'nowrap', overflow: 'hidden' }}>
          <Link to="/" style={{ color: '#e2e8f0' }}>
            📈 {collapsed ? 'DSA' : 'DailyStockAI'}
          </Link>
        </div>
        <Menu theme="dark" mode="inline" items={MENU} />
      </Sider>
      <Layout>
        <Header
          style={{
            background: '#162138',
            display: 'flex',
            justifyContent: 'space-between',
            alignItems: 'center',
            paddingInline: 24,
          }}
        >
          <Space size="middle">
            <Button
              type="text"
              style={{ color: '#cbd5e1' }}
              onClick={() => setCollapsed(!collapsed)}
              aria-label="折叠侧栏"
            >
              ☰
            </Button>
            <Typography.Text type="secondary">
              交易日：<Typography.Text>{new Date().toLocaleDateString('zh-CN')}</Typography.Text>
            </Typography.Text>
            <Typography.Text type="secondary">
              模型：<Typography.Text type="success">{model}</Typography.Text>
            </Typography.Text>
            <Typography.Text type="secondary">
              采集任务：
              <Tag color={lastTask === '运行中' ? 'processing' : 'success'}>{lastTask}</Tag>
            </Typography.Text>
          </Space>
          <Space size="middle">
            <Button type="primary" loading={collecting} onClick={triggerCollect}>
              手动触发采集
            </Button>
            <Typography.Text type="secondary">{user?.username}</Typography.Text>
            <Typography.Link onClick={logout}>退出</Typography.Link>
          </Space>
        </Header>
        <Content style={{ padding: 24, background: '#0b1120' }}>
          <Outlet />
        </Content>
      </Layout>
    </Layout>
  )
}
