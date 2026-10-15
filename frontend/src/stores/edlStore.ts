import { create } from 'zustand'
import { apiError } from '../api/backend'
import { pipelineApi } from '../api/client'
import type { EDL, EdlResponse, OutputInterval, Suggestion } from '../api/types'

interface EdlState {
  edl: EDL | null
  revisionId: string | null
  intervals: OutputInterval[]
  expectedDurationUs: number
  canUndo: boolean
  canRedo: boolean
  loading: boolean
  error: string | null
  load: (projectId: string, revisionId?: string) => Promise<void>
  applyOperation: (projectId: string, operation: Record<string, unknown>) => Promise<boolean>
  setDecisions: (
    projectId: string,
    suggestionIds: string[],
    status: 'accepted' | 'rejected' | 'pending',
  ) => Promise<boolean>
  undo: (projectId: string) => Promise<boolean>
  redo: (projectId: string) => Promise<boolean>
  reset: () => void
}

const empty = {
  edl: null,
  revisionId: null,
  intervals: [],
  expectedDurationUs: 0,
  canUndo: false,
  canRedo: false,
  loading: false,
  error: null,
}

function fromResponse(res: EdlResponse) {
  return {
    edl: res.edl,
    revisionId: res.revision_id,
    intervals: res.output_intervals,
    expectedDurationUs: res.expected_duration_us,
    canUndo: res.can_undo,
    canRedo: res.can_redo,
    error: null,
  }
}

export const useEDLStore = create<EdlState>((set, get) => ({
  ...empty,

  load: async (projectId, revisionId) => {
    set({ loading: true, error: null })
    try {
      set(fromResponse(await pipelineApi.edl(projectId, revisionId)))
    } catch (err) {
      const message = apiError(err)
      if (message.includes('尚无剪辑记录')) {
        // 新项目还没分析过：这是空状态而不是错误，不该弹红色告警
        set({ ...empty })
        return
      }
      set({ error: message })
    } finally {
      set({ loading: false })
    }
  },

  applyOperation: async (projectId, operation) => {
    const expected = get().revisionId
    set({ loading: true, error: null })
    try {
      set(fromResponse(await pipelineApi.applyOperation(projectId, operation, expected ?? undefined)))
      return true
    } catch (err) {
      const message = apiError(err)
      set({ error: message })
      // 版本冲突说明别的窗口改过，直接拉最新版本而不是继续显示旧数据
      if (message.includes('版本已变化')) await get().load(projectId)
      return false
    } finally {
      set({ loading: false })
    }
  },

  setDecisions: async (projectId, suggestionIds, status) => {
    const expected = get().revisionId
    set({ loading: true, error: null })
    try {
      set(fromResponse(await pipelineApi.setDecisions(projectId, suggestionIds, status, expected ?? undefined)))
      return true
    } catch (err) {
      const message = apiError(err)
      set({ error: message })
      if (message.includes('版本已变化')) await get().load(projectId)
      return false
    } finally {
      set({ loading: false })
    }
  },

  undo: async (projectId) => {
    set({ error: null })
    try {
      set(fromResponse(await pipelineApi.undo(projectId)))
      return true
    } catch (err) {
      set({ error: apiError(err) })
      return false
    }
  },

  redo: async (projectId) => {
    set({ error: null })
    try {
      set(fromResponse(await pipelineApi.redo(projectId)))
      return true
    } catch (err) {
      set({ error: apiError(err) })
      return false
    }
  },

  reset: () => set({ ...empty }),
}))

export const pendingSuggestions = (edl: EDL | null): Suggestion[] =>
  (edl?.suggestions ?? []).filter((s) => s.status === 'pending')
