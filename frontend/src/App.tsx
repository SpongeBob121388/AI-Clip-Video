import { useEffect } from 'react'
import { BrowserRouter, Routes, Route, useNavigate, useLocation } from 'react-router-dom'
import { Layout, Typography, Tag, Space, Menu, Alert } from 'antd'
import type { MenuProps } from 'antd'
import { checkHealth } from './api/client'
import { useAppStore } from './stores/appStore'
import ProjectListPage from './pages/ProjectListPage'
import ProjectWorkspace from './pages/ProjectWorkspace'
import SettingsPage from './pages/SettingsPage'
import AvatarStudio from './pages/AvatarStudio'

const { Header, Content, Footer } = Layout
const { Title, Text } = Typography

const menuItems: MenuProps['items'] = [
  { key: '/', label: '项目列表' },
  { key: '/workspace', label: '工作台' },
  { key: '/avatar', label: '数字人' },
  { key: '/settings', label: '设置' },
]

const HEALTH_POLL_MS = 10_000

interface AppProps {
  tokenConfigured?: boolean
}

function AppContent() {
  const { backendConnected, version, setBackend } = useAppStore()
  const navigate = useNavigate()
  const location = useLocation()

  useEffect(() => {
    let disposed = false
    let failures = 0
    const ping = async () => {
      try {
        const health = await checkHealth()
        if (disposed) return
        failures = 0
        setBackend(true, health.version)
      } catch {
        if (disposed) return
        // 单次抖动就把指示灯翻红会让人以为服务挂了，连续两次失败才算断开
        failures += 1
        if (failures >= 2) setBackend(false)
      }
    }
    void ping()
    // 后端可能比前端晚起（桌面外壳拉起 sidecar），必须轮询而不是只探一次
    const timer = window.setInterval(ping, HEALTH_POLL_MS)
    return () => {
      disposed = true
      window.clearInterval(timer)
    }
  }, [setBackend])

  return (
    <Layout style={{ minHeight: '100vh' }}>
      <Header style={{ display: 'flex', alignItems: 'center', gap: '24px' }}>
        <Title level={3} style={{ color: 'white', margin: 0, flexShrink: 0 }}>
          AI Clip Video
        </Title>
        <Menu
          theme="dark"
          mode="horizontal"
          selectedKeys={[location.pathname]}
          items={menuItems}
          onClick={({ key }) => navigate(key)}
          style={{ flex: 1, minWidth: 0 }}
        />
        <Space style={{ flexShrink: 0 }}>
          <Tag color={backendConnected ? 'green' : 'red'}>
            后端: {backendConnected ? 'ok' : '未连接'}
          </Tag>
        </Space>
      </Header>
      <Content style={{ padding: '24px' }}>
        {!backendConnected && (
          <Alert
            type="warning"
            showIcon
            style={{ marginBottom: 16 }}
            message="后端未连接"
            description="请确认本机服务已启动（python -m app.main），且前端取得了访问令牌。"
          />
        )}
        <Routes>
          <Route path="/" element={<ProjectListPage />} />
          <Route path="/workspace" element={<ProjectWorkspace />} />
          <Route path="/avatar" element={<AvatarStudio />} />
          <Route path="/settings" element={<SettingsPage />} />
        </Routes>
      </Content>
      <Footer style={{ textAlign: 'center' }}>
        <Space>
          <Text>AI Clip Video ©2026</Text>
          {version && <Text type="secondary">v{version}</Text>}
        </Space>
      </Footer>
    </Layout>
  )
}

function App({ tokenConfigured = true }: AppProps) {
  const setTokenConfigured = useAppStore((s) => s.setTokenConfigured)

  useEffect(() => {
    setTokenConfigured(tokenConfigured)
  }, [tokenConfigured, setTokenConfigured])

  return (
    <BrowserRouter>
      <AppContent />
    </BrowserRouter>
  )
}

export default App
