import { ConfigProvider, theme } from 'antd'
import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom'
import AppLayout from './components/AppLayout'
import RequireAuth from './components/RequireAuth'
import LoginPage from './pages/LoginPage'
import DashboardPage from './pages/DashboardPage'
import {
  AfternoonPage,
  BacktestPage,
  ConfigPage,
  HotPage,
  MorningPage,
  NewsPage,
  SelectorPage,
  TaskPage,
  WatchPage,
} from './pages/placeholders'

// 深色主题对齐原型（docs/AI投研工作台.html：#0b1120/#162138/#273757/#2563eb）
export default function App() {
  return (
    <ConfigProvider
      theme={{
        algorithm: theme.darkAlgorithm,
        token: {
          colorPrimary: '#2563eb',
          colorBgBase: '#0b1120',
          colorBgContainer: '#162138',
          colorBorderSecondary: '#273757',
          borderRadius: 8,
        },
      }}
    >
      <BrowserRouter>
        <Routes>
          <Route path="/login" element={<LoginPage />} />
          <Route
            element={
              <RequireAuth>
                <AppLayout />
              </RequireAuth>
            }
          >
            <Route path="/" element={<DashboardPage />} />
            <Route path="/morning" element={<MorningPage />} />
            <Route path="/afternoon" element={<AfternoonPage />} />
            <Route path="/selector" element={<SelectorPage />} />
            <Route path="/backtest" element={<BacktestPage />} />
            <Route path="/hot" element={<HotPage />} />
            <Route path="/watch" element={<WatchPage />} />
            <Route path="/news" element={<NewsPage />} />
            <Route path="/task" element={<TaskPage />} />
            <Route path="/config" element={<ConfigPage />} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Route>
        </Routes>
      </BrowserRouter>
    </ConfigProvider>
  )
}
