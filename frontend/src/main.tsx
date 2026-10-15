import React from 'react'
import ReactDOM from 'react-dom/client'
import { ConfigProvider } from 'antd'
import zhCN from 'antd/locale/zh_CN'
import { bootstrapBackend, type BackendInfo } from './api/backend'
import App from './App.tsx'
import './index.css'

const theme = {
  token: {
    colorPrimary: '#1677ff',
    borderRadius: 6,
  },
}

// message/modal 等静态方法挂在 React 树外，不声明 holderRender 就吃不到这里的主题
ConfigProvider.config({ holderRender: (children) => <ConfigProvider theme={theme}>{children}</ConfigProvider> })

const root = ReactDOM.createRoot(document.getElementById('root')!)

function renderApp(info: BackendInfo) {
  root.render(
    <React.StrictMode>
      <ConfigProvider locale={zhCN} theme={theme}>
        <App tokenConfigured={Boolean(info.token)} />
      </ConfigProvider>
    </React.StrictMode>,
  )
}

function renderStartupError(message: string) {
  // 起不来也要有字可看：空白窗口 + 只在控制台里的 rejection，对用户的观感就是「打不开」
  root.render(
    <div style={{ padding: 32, fontFamily: 'system-ui', lineHeight: 1.7, color: '#333' }}>
      <h2 style={{ margin: '0 0 8px' }}>本地服务未能启动</h2>
      <p style={{ margin: 0 }}>{message}</p>
    </div>,
  )
}

function startupMessage(err: unknown): string {
  // Rust 侧的 Err(String) 到 JS 里可能是裸字符串，也可能被包成 Error，两种都要能显示
  if (typeof err === 'string' && err) return err
  if (err instanceof Error && err.message) return err.message
  return '未能取得本地服务地址，请重新启动应用'
}

bootstrapBackend()
  .then(renderApp)
  .catch((err) => renderStartupError(startupMessage(err)))
