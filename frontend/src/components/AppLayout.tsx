import { Layout, Menu, Typography } from 'antd'
import { Link, NavLink, Outlet, useNavigate } from 'react-router-dom'
import { authApi } from '../api/client'
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

export default function AppLayout() {
  const navigate = useNavigate()
  const user = useCurrentUser()

  const logout = async () => {
    try {
      await authApi.logout()
    } finally {
      navigate('/login', { replace: true })
    }
  }

  return (
    <Layout style={{ minHeight: '100vh' }}>
      <Sider theme="dark" width={220}>
        <div style={{ padding: 16, color: '#e2e8f0', fontWeight: 700 }}>
          <Link to="/" style={{ color: '#e2e8f0' }}>
            📈 DailyStockAI
          </Link>
        </div>
        <Menu theme="dark" mode="inline" items={MENU} />
      </Sider>
      <Layout>
        <Header
          style={{
            background: '#162138',
            display: 'flex',
            justifyContent: 'flex-end',
            alignItems: 'center',
            gap: 16,
          }}
        >
          <Typography.Text type="secondary">{user?.username}</Typography.Text>
          <Typography.Link onClick={logout}>退出</Typography.Link>
        </Header>
        <Content style={{ padding: 24, background: '#0b1120' }}>
          <Outlet />
        </Content>
      </Layout>
    </Layout>
  )
}
