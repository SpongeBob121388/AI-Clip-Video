import { create } from 'zustand'
import { apiError } from '../api/backend'
import { pipelineApi, settingsApi } from '../api/client'
import type { Capabilities, SettingsResponse } from '../api/types'

interface SettingsState {
  asrEngine: string
  asrModelSize: string
  llmBaseUrl: string
  hasLlmApiKey: boolean
  hasAvatarApiKey: boolean
  credentialPersistent: boolean
  environment: string
  capabilities: Capabilities | null
  loaded: boolean
  error: string | null
  hydrate: () => Promise<void>
  setAsrEngine: (engine: string) => void
  setAsrModelSize: (size: string) => void
  setLlmBaseUrl: (url: string) => void
  save: (payload: { llm_api_key?: string; avatar_api_key?: string }) => Promise<string>
}

export const useSettingsStore = create<SettingsState>((set, get) => ({
  asrEngine: 'faster_whisper',
  asrModelSize: 'small',
  llmBaseUrl: '',
  hasLlmApiKey: false,
  hasAvatarApiKey: false,
  credentialPersistent: true,
  environment: 'production',
  capabilities: null,
  loaded: false,
  error: null,

  // 机密不会从后端回读，因此这里只恢复可明文落库的偏好与"是否已配置"标记
  hydrate: async () => {
    set({ error: null })
    try {
      const [settings, capabilities]: [SettingsResponse, Capabilities] = await Promise.all([
        settingsApi.read(),
        pipelineApi.capabilities(),
      ])
      set({
        asrEngine: settings.asr_engine,
        asrModelSize: settings.asr_model_size,
        llmBaseUrl: settings.llm_base_url ?? '',
        hasLlmApiKey: settings.has_llm_api_key,
        hasAvatarApiKey: settings.has_avatar_api_key,
        credentialPersistent: settings.credential_persistent,
        environment: settings.environment,
        capabilities,
        loaded: true,
      })
    } catch (err) {
      set({ error: apiError(err), loaded: true })
    }
  },

  setAsrEngine: (engine) => set({ asrEngine: engine }),
  setAsrModelSize: (size) => set({ asrModelSize: size }),
  setLlmBaseUrl: (url) => set({ llmBaseUrl: url }),

  save: async (secrets) => {
    const { asrEngine, asrModelSize, llmBaseUrl } = get()
    const payload: Record<string, unknown> = {
      asr_engine: asrEngine,
      asr_model_size: asrModelSize,
      llm_base_url: llmBaseUrl,
    }
    if (secrets.llm_api_key) payload.llm_api_key = secrets.llm_api_key
    if (secrets.avatar_api_key) payload.avatar_api_key = secrets.avatar_api_key
    const res = await settingsApi.save(payload)
    set({ credentialPersistent: res.credential_persistent })
    return res.message
  },
}))
