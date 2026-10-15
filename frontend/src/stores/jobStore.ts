import { create } from 'zustand'

interface Job {
  job_id: string
  stage: string
  status: string
  progress: number
  message: string
}

interface JobState {
  activeJobs: Job[]
  addJob: (job: Job) => void
  updateJob: (jobId: string, updates: Partial<Job>) => void
  removeJob: (jobId: string) => void
}

export const useJobStore = create<JobState>((set) => ({
  activeJobs: [],
  addJob: (job) => set((state) => ({ activeJobs: [...state.activeJobs, job] })),
  updateJob: (jobId, updates) =>
    set((state) => ({
      activeJobs: state.activeJobs.map((j) =>
        j.job_id === jobId ? { ...j, ...updates } : j,
      ),
    })),
  removeJob: (jobId) =>
    set((state) => ({ activeJobs: state.activeJobs.filter((j) => j.job_id !== jobId) })),
}))