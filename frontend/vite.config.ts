import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// 仓库没有安装 @types/node，这里只声明配置文件真正用到的那一个字段
declare const process: { env: Record<string, string | undefined> }

const backendPort = process.env.ACV_PORT || '8765'

export default defineConfig({
  plugins: [react()],
  clearScreen: false,
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      '/api': {
        target: `http://127.0.0.1:${backendPort}`,
        changeOrigin: true,
      },
      '/health': {
        target: `http://127.0.0.1:${backendPort}`,
        changeOrigin: true,
      },
      '/ws': {
        target: `ws://127.0.0.1:${backendPort}`,
        ws: true,
      },
    },
  },
  envPrefix: ['VITE_', 'TAURI_'],
  build: {
    target: 'esnext',
    outDir: 'dist',
  },
})