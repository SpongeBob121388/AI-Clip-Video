import { useCallback, useEffect, useRef, useState } from 'react'
import { backendInfo } from '../api/backend'
import { jobsApi } from '../api/client'
import type { Job } from '../api/types'

const POLL_INTERVAL_MS = 800
const WS_RECONNECT_LIMIT = 3

interface WatchState {
  job: Job | null
  error: string | null
}

/**
 * 盯一个后台任务：WebSocket 推进度，轮询拿终态。
 *
 * 两条通道是必要的 —— 后端的 WS 只推 {job_id, progress, message}，终态与结果只在
 * REST 里；反过来只轮询会让渲染进度看起来一格一跳。任一通道断开都不应让 UI 卡死。
 */
export function useJobWatch(jobId: string | null) {
  const [state, setState] = useState<WatchState>({ job: null, error: null })
  const wsRef = useRef<WebSocket | null>(null)
  const progressRef = useRef<{ progress: number; message: string }>({ progress: 0, message: '' })
  const terminalRef = useRef(false)

  const cancel = useCallback(async () => {
    if (!jobId) return
    try {
      const job = await jobsApi.cancel(jobId)
      setState((prev) => ({ ...prev, job: job.data }))
    } catch (err) {
      setState((prev) => ({ ...prev, error: (err as Error).message }))
    }
  }, [jobId])

  useEffect(() => {
    if (!jobId) {
      setState({ job: null, error: null })
      return
    }
    terminalRef.current = false
    progressRef.current = { progress: 0, message: '' }
    setState({ job: {
      job_id: jobId,
      project_id: '',
      stage: '',
      status: 'pending',
      progress: 0,
      message: '',
      cancel_requested: false,
    }, error: null })

    let closed = false
    let attempts = 0
    let socket: WebSocket | null = null

    const safeSend = (payload: unknown) => {
      if (socket && socket.readyState === WebSocket.OPEN) {
        socket.send(JSON.stringify(payload))
      }
    }

    const connect = () => {
      const info = backendInfo()
      if (!info) return
      socket = new WebSocket(`${info.wsBase}?token=${encodeURIComponent(info.token)}`)
      socket.onopen = () => safeSend({ action: 'subscribe', job_id: jobId })
      socket.onmessage = (event) => {
        try {
          const data = JSON.parse(event.data as string)
          if (data.job_id === jobId && typeof data.progress === 'number') {
            progressRef.current = { progress: data.progress, message: String(data.message ?? '') }
            setState((prev) => prev.job && !terminalRef.current
              ? { ...prev, job: { ...prev.job, progress: data.progress, message: data.message ?? prev.job.message } }
              : prev)
          }
        } catch {
          /* 忽略非 JSON 的心跳帧 */
        }
      }
      socket.onclose = () => {
        if (!closed && !terminalRef.current && attempts < WS_RECONNECT_LIMIT) {
          attempts += 1
          window.setTimeout(connect, 400 * attempts)
        }
      }
      socket.onerror = () => socket?.close()
    }
    connect()

    const timer = window.setInterval(async () => {
      if (closed || terminalRef.current) return
      try {
        const job = await jobsApi.get(jobId)
        if (closed) return
        const merged: Job = {
          ...job,
          progress: Math.max(job.progress, job.status === 'completed' ? 1 : progressRef.current.progress),
          message: job.message || progressRef.current.message,
        }
        if (['completed', 'failed', 'cancelled'].includes(job.status)) {
          terminalRef.current = true
          window.clearInterval(timer)
        }
        setState({ job: merged, error: null })
      } catch (err) {
        if (!closed) setState((prev) => ({ ...prev, error: (err as Error).message }))
      }
    }, POLL_INTERVAL_MS)

    return () => {
      closed = true
      window.clearInterval(timer)
      // 只有 OPEN 状态才发退订，否则浏览器会在控制台抛"CLOSING/CLOSED"噪声
      safeSend({ action: 'unsubscribe', job_id: jobId })
      socket?.close()
      wsRef.current = socket
    }
  }, [jobId])

  return { ...state, cancel }
}
