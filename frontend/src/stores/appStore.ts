import { create } from 'zustand'

interface AppState {
  backendConnected: boolean
  bootError: string | null
  tokenConfigured: boolean
  version: string
  setBackend: (connected: boolean, version?: string) => void
  setBootError: (error: string | null) => void
  setTokenConfigured: (configured: boolean) => void
}

export const useAppStore = create<AppState>((set) => ({
  backendConnected: false,
  bootError: null,
  tokenConfigured: true,
  version: '',
  setBackend: (connected, version) =>
    set((state) => ({ backendConnected: connected, version: version ?? state.version })),
  setBootError: (error) => set({ bootError: error }),
  setTokenConfigured: (configured) => set({ tokenConfigured: configured }),
}))
