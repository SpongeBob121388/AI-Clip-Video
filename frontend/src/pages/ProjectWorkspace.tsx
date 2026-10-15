import { useCallback, useEffect, useRef, useState } from 'react'
import {
  Alert,
  Button,
  Card,
  Divider,
  Empty,
  InputNumber,
  Progress,
  Select,
  Space,
  Switch,
  Tooltip,
  Typography,
  message,
} from 'antd'
import {
  ExperimentOutlined,
  PlayCircleOutlined,
  RedoOutlined,
  UndoOutlined,
  VideoCameraOutlined,
} from '@ant-design/icons'
import { apiError } from '../api/backend'
import { mediaApi, pipelineApi, resolveTicketUrl } from '../api/client'
import type {
  AnalyzeResult,
  Capabilities,
  Job,
  MediaProbe,
  RenderResult,
  SubtitleConfig,
} from '../api/types'
import { useEDLStore } from '../stores/edlStore'
import { useJobWatch } from '../hooks/useJobWatch'
import { useProjectStore } from '../stores/projectStore'
import MediaImportPanel from '../components/MediaImportPanel'
import SuggestionReview from '../components/SuggestionReview'
import SubtitleConfigPanel, { defaultSubtitleConfig } from '../components/SubtitleConfigPanel'
import SubtitleTextEditor from '../components/SubtitleTextEditor'
import TimelineEditor from '../components/TimelineEditor'
import ExportDialog, { defaultRenderOptions, type RenderOptions } from '../components/ExportDialog'

const { Title, Text } = Typography

const LANGUAGE_OPTIONS = [
  { value: 'auto', label: '自动检测' },
  { value: 'zh', label: '中文' },
  { value: 'en', label: '英文' },
  { value: 'ja', label: '日文' },
  { value: 'ko', label: '韩文' },
]

/** 任务失败/取消必须显式出现在界面上，不能只留一个空预览区。 */
function jobFailure(job: Job | null, label: string): string | null {
  if (!job) return null
  if (job.status === 'failed') return `${label}失败：${job.message || '未知原因'}`
  if (job.status === 'cancelled') return `${label}已取消`
  return null
}

const renderOptionsKey = (projectId: string) => `acv.renderOptions.${projectId}`

/** 识别、预览、导出必须用同一条音轨：切个页面就把选择重置回第 1 条，是个静默错轨的陷阱。 */
function loadRenderOptions(projectId: string): RenderOptions {
  try {
    const raw = localStorage.getItem(renderOptionsKey(projectId))
    if (!raw) return defaultRenderOptions
    return { ...defaultRenderOptions, ...(JSON.parse(raw) as Partial<RenderOptions>) }
  } catch {
    return defaultRenderOptions
  }
}

function ProjectWorkspace() {
  const project = useProjectStore((s) => s.currentProject)
  const edlState = useEDLStore()
  const [useAsr, setUseAsr] = useState(true)
  const [minSilenceMs, setMinSilenceMs] = useState(500)
  const [dbfsThreshold, setDbfsThreshold] = useState(-40)
  const [language, setLanguage] = useState('zh')
  const [burnSubtitles, setBurnSubtitles] = useState(false)
  const [analyzeJobId, setAnalyzeJobId] = useState<string | null>(null)
  const [previewJobId, setPreviewJobId] = useState<string | null>(null)
  const [previewUrl, setPreviewUrl] = useState<string | null>(null)
  const [sourceUrl, setSourceUrl] = useState<string | null>(null)
  const [subtitleConfig, setSubtitleConfig] = useState<SubtitleConfig>(defaultSubtitleConfig)
  const [renderOptions, setRenderOptions] = useState<RenderOptions>(defaultRenderOptions)
  const [capabilities, setCapabilities] = useState<Capabilities | null>(null)
  const [probe, setProbe] = useState<MediaProbe | null>(null)
  const [hasTranscript, setHasTranscript] = useState(false)
  const [transcriptToken, setTranscriptToken] = useState(0)
  const lastTicketPath = useRef<string>('')
  const previewUrlRef = useRef<string>('')
  const sourceVideoRef = useRef<HTMLVideoElement | null>(null)
  const analyze = useJobWatch(analyzeJobId)
  const preview = useJobWatch(previewJobId)
  const projectId = project?.project_id ?? ''

  useEffect(() => {
    if (!project) return
    void edlState.load(project.project_id)
  }, [project])

  useEffect(() => {
    pipelineApi
      .capabilities()
      .then(setCapabilities)
      .catch(() => setCapabilities(null))
  }, [])

  // 先读后写：不这样排序的话，进入项目的第一次渲染会把默认值写回缓存，刚选好的音轨被抹掉
  const optionsLoadedFor = useRef<string>('')
  useEffect(() => {
    if (!projectId || optionsLoadedFor.current === projectId) return
    optionsLoadedFor.current = projectId
    setRenderOptions(loadRenderOptions(projectId))
  }, [projectId])

  useEffect(() => {
    if (!projectId || optionsLoadedFor.current !== projectId) return
    localStorage.setItem(renderOptionsKey(projectId), JSON.stringify(renderOptions))
  }, [projectId, renderOptions])

  // 打开老项目时也要知道有几条音轨：探测只在导入面板里做过一次的话，选轨控件会是空的
  useEffect(() => {
    const sourcePath = project?.source_path
    if (!sourcePath) {
      setProbe(null)
      return
    }
    mediaApi.probe(sourcePath).then(setProbe).catch(() => setProbe(null))
  }, [project?.source_path])

  const audioTracks = probe ? probe.tracks.filter((t) => t.codec_type === 'audio') : []
  const trackCount = audioTracks.length

  // 音轨数变少时把选择夹回来，否则会把一个越界的下标发给后端换 409
  useEffect(() => {
    if (trackCount > 0 && renderOptions.audioTrackIndex >= trackCount) {
      setRenderOptions({ ...renderOptions, audioTrackIndex: 0 })
    }
  }, [trackCount, renderOptions])

  const selectedTrack = trackCount > 1 ? renderOptions.audioTrackIndex : null

  // 字幕产物依赖转录结果，可用性只以服务端回答为准，不靠前端猜
  const refreshTranscript = useCallback(async () => {
    if (!projectId) return
    try {
      const transcript = await pipelineApi.transcript(projectId)
      setHasTranscript(transcript.available && transcript.utterance_count > 0)
    } catch {
      setHasTranscript(false)
    }
  }, [projectId])

  useEffect(() => {
    void refreshTranscript()
  }, [refreshTranscript])

  useEffect(() => {
    if (analyze.error) setPreviewUrl(null)
  }, [analyze.error])

  const openPreview = useCallback(async (path: string) => {
    if (lastTicketPath.current === path && previewUrlRef.current) return
    try {
      const ticket = await mediaApi.streamTicket(path)
      lastTicketPath.current = path
      previewUrlRef.current = resolveTicketUrl(ticket.url)
      setPreviewUrl(previewUrlRef.current)
    } catch (err) {
      message.error(apiError(err))
    }
  }, [])

  const handleRendered = useCallback(
    (path: string) => {
      void openPreview(path)
    },
    [openPreview],
  )

  const playSource = useCallback(async () => {
    if (!project?.source_path) return
    try {
      const ticket = await mediaApi.sourceTicket(project.project_id)
      setSourceUrl(resolveTicketUrl(ticket.url))
    } catch (err) {
      message.error(apiError(err))
    }
  }, [project])

  // 时间轴的播放头要跟着原片走，所以原片播放器必须一直在：进项目就自动取一次流
  useEffect(() => {
    if (project?.source_path) void playSource()
  }, [project?.project_id, project?.source_path, playSource])

  // 分析任务终态：重新读取服务端 EDL（唯一权威），否则建议列表停留在旧版本
  useEffect(() => {
    const status = analyze.job?.status
    if (!project || !status) return
    if (status === 'completed' || status === 'failed' || status === 'cancelled') {
      void edlState.load(project.project_id)
      void refreshTranscript()
      setTranscriptToken((value) => value + 1)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [analyze.job?.status, project, refreshTranscript])

  // 预览任务终态：成功则取票据播放，失败则把后端原因原样显示（不假装完成）
  const previewPath =
    preview.job?.status === 'completed'
      ? String((preview.job.result as unknown as RenderResult | null)?.output_path ?? '')
      : ''
  const previewTerminal = preview.job ? ['completed', 'failed', 'cancelled'].includes(preview.job.status) : false

  useEffect(() => {
    if (!previewTerminal) return
    if (preview.job?.status !== 'completed') {
      setPreviewUrl(null)
      return
    }
    if (!previewPath) return
    void openPreview(previewPath)
    // 依赖必须是原始值：轮询每 800ms 就生成新的 job 对象，依赖对象会反复签发票据
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [previewPath, previewTerminal])

  const handleAnalyze = async () => {
    if (!project) return
    setPreviewUrl(null)
    try {
      const job = await pipelineApi.analyze(project.project_id, {
        use_asr: useAsr,
        min_silence_ms: minSilenceMs,
        dbfs_threshold: dbfsThreshold,
        language,
        audio_track_index: selectedTrack,
      })
      setAnalyzeJobId(job.job_id)
    } catch (err) {
      message.error(apiError(err))
    }
  }

  const handlePreview = async () => {
    if (!project) return
    setPreviewUrl(null)
    const burn = burnSubtitles && hasTranscript
    try {
      const job = await pipelineApi.preview(
        project.project_id,
        {
          resolution: '720p',
          subtitle_mode: burn ? 'burn_in' : 'none',
          subtitle_config: burn ? subtitleConfig : null,
          aspect_ratio: '9:16',
          layout_mode: 'blur_background',
          // 预览听哪条音轨必须和导出一致，否则"听着对、导出来是另一条"
          audio_track_index: selectedTrack,
        },
        burn ? subtitleConfig : undefined,
      )
      setPreviewJobId(job.job_id)
    } catch (err) {
      message.error(apiError(err))
    }
  }

  if (!project) {
    return (
      <Card>
        <Empty description="请先从项目列表选择或创建一个项目" />
      </Card>
    )
  }

  const analyzing = analyze.job !== null && ['pending', 'running'].includes(analyze.job.status)
  const rendering = preview.job !== null && ['pending', 'running'].includes(preview.job.status)
  const analyzeResult =
    analyze.job?.status === 'completed' ? (analyze.job.result as unknown as AnalyzeResult) : null

  return (
    <Space direction="vertical" size="middle" style={{ width: '100%' }}>
      <Card>
        <Space style={{ justifyContent: 'space-between', width: '100%' }} wrap>
          <Space direction="vertical" size={0}>
            <Title level={4} style={{ margin: 0 }}>
              {project.name}
            </Title>
            <Text type="secondary">
              {project.source_path ?? '未绑定媒体文件'}
              {project.duration_us ? ` · ${(project.duration_us / 1_000_000).toFixed(1)}s` : ''}
            </Text>
          </Space>
          <Space wrap>
            <Button
              icon={<VideoCameraOutlined />}
              disabled={!project.source_path}
              onClick={() => void playSource()}
            >
              播放原片
            </Button>
            <Button
              type="primary"
              icon={<ExperimentOutlined />}
              loading={analyzing}
              disabled={!project.source_path}
              onClick={handleAnalyze}
            >
              开始分析
            </Button>
            <Button
              icon={<PlayCircleOutlined />}
              loading={rendering}
              disabled={!edlState.edl}
              onClick={handlePreview}
            >
              预览
            </Button>
            <Tooltip title="撤销上一步剪辑">
              <Button
                icon={<UndoOutlined />}
                disabled={!edlState.canUndo || edlState.loading}
                onClick={() => void edlState.undo(project.project_id)}
              >
                撤销
              </Button>
            </Tooltip>
            <Tooltip title="重做被撤销的剪辑">
              <Button
                icon={<RedoOutlined />}
                disabled={!edlState.canRedo || edlState.loading}
                onClick={() => void edlState.redo(project.project_id)}
              >
                重做
              </Button>
            </Tooltip>
          </Space>
        </Space>

        <Divider style={{ margin: '12px 0' }} />

        <Space wrap size="middle" align="end">
          <Space size={4}>
            <Switch size="small" checked={useAsr} onChange={setUseAsr} />
            <Text type="secondary">语音识别</Text>
          </Space>
          <Space size={4}>
            <Tooltip title={hasTranscript ? '预览时把字幕烧进画面' : '需要语音识别结果，先打开「语音识别」并分析'}>
              <Switch
                size="small"
                checked={burnSubtitles && hasTranscript}
                disabled={!hasTranscript}
                onChange={(checked) => setBurnSubtitles(hasTranscript && checked)}
              />
            </Tooltip>
            <Text type="secondary">预览烧录字幕</Text>
          </Space>
          <Space size={4}>
            <Text type="secondary">最小静音</Text>
            <InputNumber
              size="small"
              min={100}
              max={10_000}
              step={50}
              value={minSilenceMs}
              onChange={(v) => setMinSilenceMs(v ?? 500)}
              style={{ width: 90 }}
            />
            <Text type="secondary">ms</Text>
          </Space>
          <Space size={4}>
            <Text type="secondary">音量阈值</Text>
            <InputNumber
              size="small"
              min={-120}
              max={0}
              step={1}
              value={dbfsThreshold}
              onChange={(v) => setDbfsThreshold(v ?? -40)}
              style={{ width: 80 }}
            />
            <Text type="secondary">dBFS</Text>
          </Space>
          <Space size={4}>
            <Text type="secondary">识别语言</Text>
            <Select
              size="small"
              value={language}
              onChange={setLanguage}
              style={{ width: 110 }}
              options={LANGUAGE_OPTIONS}
            />
          </Space>
          {trackCount > 1 && (
            <Space size={4}>
              <Tooltip title="识别与导出使用同一条音轨；换轨后请重新分析，否则字幕仍来自旧的轨">
                <Text type="secondary">音轨</Text>
              </Tooltip>
              <Select
                size="small"
                value={renderOptions.audioTrackIndex}
                onChange={(audioTrackIndex) => setRenderOptions({ ...renderOptions, audioTrackIndex })}
                style={{ width: 150 }}
                options={audioTracks.map((track, index) => ({
                  value: index,
                  label: `第 ${index + 1} 条 · ${track.codec_name}`,
                }))}
              />
            </Space>
          )}
        </Space>

        {analyzing && (
          <Progress
            percent={Math.round((analyze.job?.progress ?? 0) * 100)}
            status="active"
            format={() => analyze.job?.message || '处理中'}
          />
        )}
        {analyzeResult && (
          <Alert
            style={{ marginTop: 12 }}
            type={analyzeResult.suggestion_count > 0 ? 'success' : 'info'}
            showIcon
            message={
              analyzeResult.suggestion_count > 0
                ? `生成 ${analyzeResult.suggestion_count} 条建议（候选 ${analyzeResult.candidate_count} 段）`
                : '未检测到可剪掉的静音段'
            }
            description={
              <Space direction="vertical" size={2}>
                {analyzeResult.asr_skipped_reason && (
                  <Text type="secondary">已跳过语音识别：{analyzeResult.asr_skipped_reason}</Text>
                )}
                {analyzeResult.used_rule_fallback && (
                  <Text type="secondary">LLM 不可用，已按规则策略给出建议</Text>
                )}
                {analyzeResult.utterance_count > 0 && (
                  <Text type="secondary">识别到 {analyzeResult.utterance_count} 句</Text>
                )}
              </Space>
            }
          />
        )}
      </Card>

      {(edlState.error ||
        analyze.error ||
        preview.error ||
        jobFailure(analyze.job, '分析') ||
        jobFailure(preview.job, '预览渲染')) && (
        <Alert
          type="error"
          showIcon
          closable
          message="操作失败"
          description={[
            edlState.error,
            analyze.error,
            preview.error,
            jobFailure(analyze.job, '分析'),
            jobFailure(preview.job, '预览渲染'),
          ]
            .filter(Boolean)
            .join('；')}
          onClose={() => edlState.load(project.project_id)}
        />
      )}

      <MediaImportPanel projectId={project.project_id} onProbe={setProbe} />

      {sourceUrl && (
        <Card
          title="原片"
          extra={<Text type="secondary">取流地址约 15 分钟有效，失效后重新点「播放原片」</Text>}
        >
          <video
            ref={sourceVideoRef}
            src={sourceUrl}
            controls
            style={{ width: '100%', maxHeight: 420 }}
            onError={() => {
              setSourceUrl(null)
              message.warning('取流地址已失效，请重新点击「播放原片」')
            }}
          />
        </Card>
      )}

      {edlState.edl && (
        <TimelineEditor
          durationUs={edlState.edl.source.duration_us}
          suggestions={edlState.edl.suggestions}
          outputIntervals={edlState.intervals}
          expectedDurationUs={edlState.expectedDurationUs}
          fps={probe?.fps ?? null}
          loading={edlState.loading}
          videoRef={sourceVideoRef}
          onAdd={(startUs, endUs) =>
            edlState.applyOperation(project.project_id, {
              operation_type: 'add',
              source_start_us: startUs,
              source_end_us: endUs,
            })
          }
          onModify={(oldStartUs, startUs, endUs) =>
            edlState.applyOperation(project.project_id, {
              operation_type: 'modify',
              old_source_start_us: oldStartUs,
              new_source_start_us: startUs,
              new_source_end_us: endUs,
            })
          }
          onRemove={(startUs, endUs) =>
            edlState.applyOperation(project.project_id, {
              operation_type: 'remove',
              source_start_us: startUs,
              source_end_us: endUs,
            })
          }
          onMerge={(firstStartUs, secondEndUs) =>
            edlState.applyOperation(project.project_id, {
              operation_type: 'merge',
              first_source_start_us: firstStartUs,
              second_source_end_us: secondEndUs,
            })
          }
          onSplit={(startUs, splitAtUs) =>
            edlState.applyOperation(project.project_id, {
              operation_type: 'split',
              source_start_us: startUs,
              split_at_us: splitAtUs,
            })
          }
          onDecide={(ids, status) => edlState.setDecisions(project.project_id, ids, status)}
        />
      )}

      {previewUrl && (
        <Card title="预览">
          <video src={previewUrl} controls style={{ width: '100%', maxHeight: 420 }} />
          {preview.job?.status === 'completed' && (
            <Text type="secondary" style={{ display: 'block', marginTop: 8 }}>
              产物：{String((preview.job.result as Record<string, unknown> | null)?.output_path ?? '')}
            </Text>
          )}
        </Card>
      )}

      <SuggestionReview
        suggestions={edlState.edl?.suggestions ?? []}
        loading={edlState.loading}
        expectedDurationUs={edlState.expectedDurationUs}
        sourceDurationUs={edlState.edl?.source.duration_us ?? 0}
        onDecide={(ids, status) => edlState.setDecisions(project.project_id, ids, status)}
        onAddInterval={(start, end) =>
          edlState.applyOperation(project.project_id, {
            operation_type: 'add',
            source_start_us: start,
            source_end_us: end,
          })
        }
      />

      <SubtitleTextEditor
        projectId={project.project_id}
        reloadToken={transcriptToken}
        onChanged={refreshTranscript}
      />

      <SubtitleConfigPanel
        value={subtitleConfig}
        onChange={setSubtitleConfig}
        projectId={project.project_id}
        hasTranscript={hasTranscript}
      />

      <ExportDialog
        projectId={project.project_id}
        subtitleConfig={subtitleConfig}
        hasTranscript={hasTranscript}
        capabilities={capabilities}
        audioTracks={audioTracks}
        renderOptions={renderOptions}
        onRenderOptionsChange={setRenderOptions}
        onRendered={handleRendered}
      />
    </Space>
  )
}

export default ProjectWorkspace
