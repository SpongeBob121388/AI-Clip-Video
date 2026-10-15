import axios, { type AxiosInstance } from 'axios'
import { backendInfo, bootstrapBackend } from './backend'
import type {
  AnalyzeResult,
  Capabilities,
  EdlResponse,
  Job,
  LLMTestResult,
  MediaProbe,
  Project,
  RenderPreset,
  RenderResult,
  Revision,
  SettingsResponse,
  StreamTicket,
  SubtitleConfig,
  Transcript,
} from './types'

let client: AxiosInstance | null = null

/**
 * 取得配置好令牌的 axios 实例。
 * 必须先 await bootstrapBackend()（应用启动时已调用），否则这里会抛错而不是静默无认证。
 */
export function api(): AxiosInstance {
  const info = backendInfo()
  if (!info) throw new Error('后端尚未引导完成')
  if (!client) {
    client = axios.create({
      baseURL: info.restBase,
      timeout: 60_000,
      headers: { 'Content-Type': 'application/json' },
    })
    client.interceptors.request.use((config) => {
      if (info.token) config.headers.Authorization = `Bearer ${info.token}`
      return config
    })
  }
  return client
}

/** 令牌轮换 / 重连后强制重建实例。 */
export function resetApiClient(): void {
  client = null
}

export async function checkHealth(): Promise<{ status: string; app: string; version: string }> {
  const info = await bootstrapBackend()
  const base = info.restBase.replace(/\/api\/v1$/, '')
  const response = await axios.get(`${base}/health`, { timeout: 5_000 })
  return response.data
}

// ------------------------------------------------------------------ 项目

export const projectsApi = {
  list: async (limit = 50): Promise<Project[]> => {
    const res = await api().get('/projects', { params: { limit } })
    return res.data.projects
  },
  get: async (id: string): Promise<Project> => (await api().get(`/projects/${id}`)).data,
  create: async (name: string, sourcePath?: string): Promise<Project> =>
    (await api().post<Project>('/projects', { name, source_path: sourcePath ?? null })).data,
  delete: (id: string) => api().delete(`/projects/${id}`),
  attachSource: async (id: string, filePath: string): Promise<Project> =>
    (await api().put<Project>(`/projects/${id}/source`, { file_path: filePath })).data,
  revisions: async (id: string): Promise<{ revisions: Revision[]; current_revision_id: string | null }> =>
    (await api().get(`/projects/${id}/revisions`)).data,
}

// ------------------------------------------------------------------ 媒体

export const mediaApi = {
  probe: async (filePath: string): Promise<MediaProbe> =>
    (await api().post<MediaProbe>('/media/probe', { file_path: filePath })).data,
  upload: async (file: File, onProgress?: (percent: number) => void): Promise<{ file_path: string; probe: MediaProbe }> => {
    const form = new FormData()
    form.append('file', file)
    const res = await api().post('/media/upload', form, {
      headers: { 'Content-Type': 'multipart/form-data' },
      onUploadProgress: (event) => {
        if (onProgress && event.total) onProgress(Math.round((event.loaded / event.total) * 100))
      },
    })
    return res.data
  },
  streamTicket: async (filePath: string): Promise<StreamTicket> =>
    (await api().post<StreamTicket>('/media/stream-ticket', { file_path: filePath })).data,
  sourceTicket: async (projectId: string): Promise<StreamTicket> =>
    (await api().post<StreamTicket>('/media/source-ticket', { project_id: projectId })).data,
}

/** 票据 URL 可能只有路径（开发代理），需要拼回当前后端的 base。 */
export function resolveTicketUrl(ticketUrl: string): string {
  if (ticketUrl.startsWith('http')) return ticketUrl
  const info = backendInfo()
  const base = info ? info.restBase.replace(/\/api\/v1$/, '') : ''
  return `${base}${ticketUrl}`
}

// ------------------------------------------------------------------ 链路

export const pipelineApi = {
  capabilities: async (): Promise<Capabilities> => (await api().get('/capabilities')).data,
  analyze: async (
    projectId: string,
    payload: {
      language?: string
      min_silence_ms?: number
      dbfs_threshold?: number
      use_asr?: boolean
      audio_track_index?: number | null
    },
  ): Promise<Job> =>
    (await api().post<Job>(`/projects/${projectId}/analyze`, payload)).data,
  edl: async (projectId: string, revisionId?: string): Promise<EdlResponse> =>
    (
      await api().get<EdlResponse>(`/projects/${projectId}/edl`, {
        params: revisionId ? { revision_id: revisionId } : {},
      })
    ).data,
  applyOperation: async (
    projectId: string,
    operation: Record<string, unknown>,
    expectedRevisionId?: string,
  ): Promise<EdlResponse> =>
    (
      await api().post<EdlResponse>(`/projects/${projectId}/edl/operations`, {
        operation,
        expected_revision_id: expectedRevisionId ?? null,
      })
    ).data,
  setDecisions: async (
    projectId: string,
    suggestionIds: string[],
    status: 'accepted' | 'rejected' | 'pending',
    expectedRevisionId?: string,
  ): Promise<EdlResponse> =>
    (
      await api().post<EdlResponse>(`/projects/${projectId}/suggestions/decisions`, {
        suggestion_ids: suggestionIds,
        status,
        expected_revision_id: expectedRevisionId ?? null,
      })
    ).data,
  undo: async (projectId: string): Promise<EdlResponse> =>
    (await api().post<EdlResponse>(`/projects/${projectId}/undo`)).data,
  redo: async (projectId: string): Promise<EdlResponse> =>
    (await api().post<EdlResponse>(`/projects/${projectId}/redo`)).data,
  transcript: async (projectId: string): Promise<Transcript> =>
    (await api().get<Transcript>(`/projects/${projectId}/transcript`)).data,
  /** 导出剪辑交换文件（.otio），产物只含剪辑决策与 file:// 媒体路径。 */
  exportOtio: async (projectId: string): Promise<{ output_path: string }> =>
    (await api().get<{ output_path: string }>(`/projects/${projectId}/otio`)).data,
  /** 写人工文案并取回套用后的转录（空文本 = 撤销该条覆盖，回到机器识别的原文）。 */
  setTranscriptOverrides: async (
    projectId: string,
    overrides: { time_key: string; text: string }[],
  ): Promise<Transcript> =>
    (await api().put<Transcript>(`/projects/${projectId}/transcript/overrides`, { overrides })).data,
  subtitles: async (
    projectId: string,
    config: SubtitleConfig,
  ): Promise<{ content: string; format: string; used_srt_fallback: boolean; line_count: number; message: string }> =>
    (
      await api().post(`/projects/${projectId}/subtitles`, {
        format: config.format,
        config,
      })
    ).data,
  preview: async (projectId: string, preset: RenderPreset, config?: SubtitleConfig): Promise<Job> =>
    (
      await api().post<Job>(`/projects/${projectId}/preview`, {
        preset,
        subtitle_config: config ?? null,
      })
    ).data,
  export: async (
    projectId: string,
    preset: RenderPreset,
    outputPath: string,
    config?: SubtitleConfig,
  ): Promise<Job> =>
    (
      await api().post<Job>(`/projects/${projectId}/export`, {
        preset,
        subtitle_config: config ?? null,
        output_path: outputPath,
      })
    ).data,
}

// ------------------------------------------------------------------ 任务

export const jobsApi = {
  list: async (status?: string): Promise<Job[]> => {
    const res = await api().get('/jobs', { params: { status } })
    return res.data.jobs
  },
  get: async (id: string): Promise<Job> => (await api().get<Job>(`/jobs/${id}`)).data,
  cancel: (id: string) => api().post(`/jobs/${id}/cancel`),
}

export function jobResult<T>(job: Job): T | null {
  return (job.result ?? null) as T | null
}

export type { AnalyzeResult, RenderResult }

// ------------------------------------------------------------------ 设置

export const settingsApi = {
  read: async (): Promise<SettingsResponse> => (await api().get('/settings')).data,
  save: async (payload: Record<string, unknown>): Promise<{ success: boolean; message: string; saved_keys: string[]; credential_persistent: boolean }> =>
    (await api().post('/settings/save', payload)).data,
  testLlm: async (payload: { api_key: string; base_url: string; model: string }): Promise<LLMTestResult> =>
    (await api().post<LLMTestResult>('/settings/llm/test', payload)).data,
}

// ------------------------------------------------------------------ 数字人

export const avatarApi = {
  capabilities: async (): Promise<{
    available: boolean
    message: string
    providers: { id: string; available: boolean }[]
  }> => (await api().get('/avatar/capabilities')).data,
}
