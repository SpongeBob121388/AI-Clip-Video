/**
 * 后端地址与访问令牌引导。
 *
 * 信任边界：FastAPI 只听环回口，且每个进程持有一枚随机令牌（data_dir/api.token）。
 * 打包后的外壳必须由 Rust 侧把端口与令牌交给前端；浏览器开发模式下走 Vite 代理，
 * 令牌由 `.env.local` 的 VITE_AUTH_TOKEN 提供（与后端 ACV_AUTH_TOKEN 同值）。
 */

const DEFAULT_PORT = 8765

export interface BackendInfo {
  restBase: string
  wsBase: string
  token: string
  source: 'tauri' | 'env'
}

export function isTauri(): boolean {
  return typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window
}

interface TauriBackendInfo {
  port: number
  token: string
}

async function readTauriBackend(): Promise<TauriBackendInfo | null> {
  if (!isTauri()) return null
  const { invoke } = await import('@tauri-apps/api/core')
  // 桌面外壳里这里不报错就是成功：错误信息带着「本地服务未就绪」与日志路径，
  // 让调用方把它显示出来。静默退回 8765 只会让用户对着空列表和 401 猜原因。
  const info = await invoke<TauriBackendInfo>('backend_info')
  if (!info || typeof info.port !== 'number' || typeof info.token !== 'string') {
    throw new Error('外壳没有给出可用的后端端口与访问令牌')
  }
  return info
}

let cached: BackendInfo | null = null

export async function bootstrapBackend(): Promise<BackendInfo> {
  if (cached) return cached

  const native = await readTauriBackend()
  const port = native?.port ?? Number(import.meta.env.VITE_BACKEND_PORT ?? DEFAULT_PORT)
  const token = native?.token ?? (import.meta.env.VITE_AUTH_TOKEN ?? '')

  // 打包后的前端由 tauri://localhost 提供，相对路径不会命中后端，必须写绝对地址；
  // 开发模式（浏览器或 tauri dev + vite）交给 Vite 代理。
  const absolute = isTauri() && import.meta.env.PROD
  const restBase = absolute ? `http://127.0.0.1:${port}/api/v1` : '/api/v1'
  const wsBase = absolute
    ? `ws://127.0.0.1:${port}/ws/progress`
    : `${window.location.protocol === 'https:' ? 'wss' : 'ws'}://${window.location.host}/ws/progress`

  cached = { restBase, wsBase, token, source: native ? 'tauri' : 'env' }
  return cached
}

export function backendInfo(): BackendInfo | null {
  return cached
}

/** 把 axios / 原生错误统一成一句可读的中文提示。 */
export function apiError(err: unknown): string {
  const anyErr = err as {
    response?: { status?: number; data?: { detail?: string } }
    message?: string
  }
  const detail = anyErr?.response?.data?.detail
  if (detail) return detail
  const status = anyErr?.response?.status
  if (status === 401) return '未取得访问令牌，请确认后端已启动（或设置 VITE_AUTH_TOKEN）'
  if (status && status >= 500) return `后端错误（HTTP ${status}）`
  return anyErr?.message || '请求失败'
}
