/** 与后端 Pydantic 模型一一对应的类型定义。 */

export interface Project {
  project_id: string
  name: string
  source_path: string | null
  duration_us: number
  current_revision_id: string | null
  created_at?: string | null
  updated_at?: string | null
}

export interface Revision {
  revision_id: string
  revision_number: number
  parent_revision_id: string | null
  operation_type: string
  created_at?: string | null
}

export type SuggestionStatus = 'pending' | 'accepted' | 'rejected' | 'modified'
export type Decision = 'delete' | 'keep' | 'uncertain'

export interface Suggestion {
  id: string
  source_start_us: number
  source_end_us: number
  suggestion_type: string
  decision: Decision
  source: string
  reason: string
  status: SuggestionStatus
  strength: string
  prev_text: string
  next_text: string
}

export interface EDLSource {
  uri: string
  duration_us: number
  time_base?: { num: number; den: number }
  rotation: number
  is_vfr: boolean
}

export interface EDLOutput {
  aspect_ratio: string
  resolution: string
  subtitle_preset_id: string
  layout_mode: string
}

export interface EDL {
  schema_version: number
  project_id: string
  source: EDLSource
  suggestions: Suggestion[]
  output: EDLOutput
}

export interface OutputInterval {
  start_us: number
  end_us: number
  source_start_us: number
}

export interface EdlResponse {
  project_id: string
  revision_id: string
  edl: EDL
  output_intervals: OutputInterval[]
  expected_duration_us: number
  can_undo: boolean
  can_redo: boolean
}

export interface MediaTrack {
  index: number
  codec_type: string
  codec_name: string
  width?: number | null
  height?: number | null
  channels?: number | null
  sample_rate?: number | null
  fps?: number | null
}

export interface MediaProbe {
  file_path: string
  duration_us: number
  width: number | null
  height: number | null
  fps: number | null
  codec_video: string | null
  codec_audio: string | null
  rotation: number
  is_vfr: boolean
  time_base: string | null
  tracks: MediaTrack[]
  bit_rate: number | null
}

/** MediaProbe 的 has_* 是 Python 属性，不会出现在 JSON 里，前端按流列表自行判断。 */
export const hasVideoTrack = (probe: MediaProbe): boolean =>
  probe.tracks.some((t) => t.codec_type === 'video')
export const hasAudioTrack = (probe: MediaProbe): boolean =>
  probe.tracks.some((t) => t.codec_type === 'audio')

export interface Job {
  job_id: string
  project_id: string
  stage: string
  status: 'pending' | 'running' | 'completed' | 'failed' | 'cancelled'
  progress: number
  message: string
  cancel_requested: boolean
  result?: Record<string, unknown> | null
}

export interface AnalyzeResult {
  project_id: string
  revision_id: string
  duration_us: number
  width: number | null
  height: number | null
  candidate_count: number
  suggestion_count: number
  utterance_count: number
  used_rule_fallback: boolean
  asr_skipped_reason: string | null
  detection_method: string
}

export interface RenderResult {
  output_path: string
  cached: boolean
  plan_hash: string
  duration_us: number
  size_bytes?: number
  width?: number | null
  height?: number | null
  subtitle_path?: string
}

export interface SubtitleStyle {
  font_name: string
  font_size: number
  primary_color: string
  outline_color: string
  bold: boolean
  italic: boolean
  outline: number
  shadow: number
  alignment: number
  margin_l: number
  margin_r: number
  margin_v: number
}

export interface SubtitleConfig {
  format: 'ass' | 'srt'
  style: SubtitleStyle
  safe_margin_horizontal: number
  safe_margin_vertical: number
  max_chars_per_line: number
}

export interface RenderPreset {
  resolution: '720p' | '1080p' | 'source'
  subtitle_mode: 'burn_in' | 'external' | 'none'
  subtitle_config?: SubtitleConfig | null
  aspect_ratio: '9:16' | '16:9'
  layout_mode: 'smart_crop' | 'blur_background'
  preview_quality?: boolean
  /** null 表示跟随源素材帧率 */
  fps?: number | null
  crf?: number
  /** null 表示第 1 条音轨 */
  audio_track_index?: number | null
}

export interface Capabilities {
  ffmpeg_available: boolean
  asr_engines: string[]
  detection_methods: string[]
  llm_available: boolean
  avatar_available: boolean
  avatar_message: string
  otio_available: boolean
  subtitle_formats: string[]
  resolutions: string[]
  crf_range: number[]
  fps_options: number[]
  transcript_editable: boolean
}

export interface SettingsResponse {
  llm_base_url: string | null
  has_llm_api_key: boolean
  has_avatar_api_key: boolean
  asr_engine: string
  asr_model_size: string
  credential_persistent: boolean
  environment: string
}

export interface LLMTestResult {
  success: boolean
  message: string
  latency_ms: number
}

export interface StreamTicket {
  ticket: string
  expires_in: number
  url: string
}

export interface TranscriptUtterance {
  utterance_id: string
  /** 人工文案的定位键：`{start_us}-{end_us}`，重跑识别后 id 会变、这个相对稳定 */
  time_key: string
  start_us: number
  end_us: number
  /** 已套用人工覆盖后的文本，界面显示与编辑的都是它 */
  text: string
  /** 机器识别的原文，用于"恢复自动文本"时让用户看到会回到哪句 */
  auto_text: string
  edited: boolean
}

export interface Transcript {
  available: boolean
  utterance_count: number
  language: string
  engine: string
  utterances: TranscriptUtterance[]
  /** 用户改过、但当前转录里找不到对应区间的文案（多半是重跑识别后切分变了） */
  unapplied_overrides: { time_key: string; text: string }[]
}
