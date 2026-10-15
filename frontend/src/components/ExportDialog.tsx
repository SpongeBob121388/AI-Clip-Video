import { useEffect, useState } from 'react'
import {
  Alert,
  Button,
  Card,
  Descriptions,
  Progress,
  Select,
  Slider,
  Space,
  Tooltip,
  Typography,
  message,
} from 'antd'
import { ExportOutlined, LinkOutlined, StopOutlined } from '@ant-design/icons'
import { apiError, isTauri } from '../api/backend'
import { mediaApi, pipelineApi, resolveTicketUrl } from '../api/client'
import type { Capabilities, MediaTrack, RenderPreset, RenderResult, SubtitleConfig } from '../api/types'
import { useJobWatch } from '../hooks/useJobWatch'

const { Text } = Typography

interface Props {
  projectId: string
  subtitleConfig: SubtitleConfig
  hasTranscript: boolean
  capabilities: Capabilities | null
  audioTracks: MediaTrack[]
  /** 帧率/CRF/音轨由父级持有：预览与 analyze 要用同一条音轨，否则字幕与画面不同源 */
  renderOptions: RenderOptions
  onRenderOptionsChange: (options: RenderOptions) => void
  onRendered?: (path: string) => void
}

export interface RenderOptions {
  fps: number | null
  crf: number
  audioTrackIndex: number
}

export const defaultRenderOptions: RenderOptions = { fps: null, crf: 18, audioTrackIndex: 0 }

async function tauriSaveAs(fileName: string): Promise<string | null> {
  const { save } = await import('@tauri-apps/plugin-dialog')
  const result = await save({
    defaultPath: fileName,
    filters: [{ name: 'MP4 视频', extensions: ['mp4'] }],
  })
  return typeof result === 'string' ? result : null
}

function ExportDialog({
  projectId,
  subtitleConfig,
  hasTranscript,
  capabilities,
  audioTracks,
  renderOptions,
  onRenderOptionsChange,
  onRendered,
}: Props) {
  const [resolution, setResolution] = useState<RenderPreset['resolution']>('1080p')
  const [subtitleMode, setSubtitleMode] = useState<RenderPreset['subtitle_mode']>('none')
  const [aspectRatio, setAspectRatio] = useState<RenderPreset['aspect_ratio']>('9:16')
  const [layoutMode, setLayoutMode] = useState<RenderPreset['layout_mode']>('blur_background')
  const [outputPath, setOutputPath] = useState<string | null>(null)
  const [jobId, setJobId] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [otioPath, setOtioPath] = useState<string | null>(null)
  const [otioBusy, setOtioBusy] = useState(false)
  const watch = useJobWatch(jobId)

  const { fps, crf, audioTrackIndex: trackIndex } = renderOptions
  const crfRange = capabilities?.crf_range ?? [14, 35]
  const fpsOptions = capabilities?.fps_options ?? [25, 30, 60]
  const patchOptions = (partial: Partial<RenderOptions>) =>
    onRenderOptionsChange({ ...renderOptions, ...partial })

  const job = watch.job
  const running = job !== null && ['pending', 'running'].includes(job.status)
  const result = job?.status === 'completed' ? (job.result as unknown as RenderResult) : null
  const exportedPath = result?.output_path ?? ''

  useEffect(() => {
    // 依赖必须是字符串：轮询每 800ms 生成新的 job 对象，依赖对象会反复触发取流票据
    if (exportedPath) onRendered?.(exportedPath)
  }, [exportedPath, onRendered])

  useEffect(() => {
    // 没有转录结果时任何字幕产物都必然失败，不让选项停在一个只会报错的值上
    if (!hasTranscript && subtitleMode !== 'none') setSubtitleMode('none')
  }, [hasTranscript, subtitleMode])

  const handleExport = async () => {
    setNotice(null)
    let target = outputPath
    if (isTauri() && !target) {
      try {
        target = await tauriSaveAs('edited.mp4')
      } catch (err) {
        // 原生对话框不可用（插件未注册）时退回服务端导出目录，而不是让按钮失灵
        setNotice(`原生保存对话框不可用，将导出到后端默认目录：${apiError(err)}`)
      }
    }
    const preset: RenderPreset = {
      resolution,
      subtitle_mode: subtitleMode,
      subtitle_config: subtitleMode === 'none' ? null : subtitleConfig,
      aspect_ratio: aspectRatio,
      layout_mode: layoutMode,
      preview_quality: false,
      fps,
      crf,
      // 单轨/无轨时不传下标：传了会在「无音轨素材」上撞出 409，而那本来是正常情况
      audio_track_index: audioTracks.length > 1 ? trackIndex : null,
    }
    try {
      const submitted = await pipelineApi.export(projectId, preset, target ?? '')
      setJobId(submitted.job_id)
      setOutputPath(target)
    } catch (err) {
      message.error(apiError(err))
    }
  }

  const handleExportOtio = async () => {
    setOtioBusy(true)
    setOtioPath(null)
    try {
      const res = await pipelineApi.exportOtio(projectId)
      setOtioPath(res.output_path)
      message.success('已导出 OTIO 剪辑工程')
    } catch (err) {
      message.error(apiError(err))
    } finally {
      setOtioBusy(false)
    }
  }

  const copyTicket = async () => {
    if (!result?.output_path) return
    try {
      const ticket = await mediaApi.streamTicket(result.output_path)
      await navigator.clipboard.writeText(resolveTicketUrl(ticket.url))
      message.success('播放地址已复制（有效期约 15 分钟）')
    } catch (err) {
      message.warning(apiError(err))
    }
  }

  return (
    <Card title="导出">
      <Space direction="vertical" style={{ width: '100%' }} size="middle">
        <Space wrap size="middle">
          <Select
            value={resolution}
            onChange={setResolution}
            style={{ width: 140 }}
            options={[
              { value: '720p', label: '720p' },
              { value: '1080p', label: '1080p' },
              { value: 'source', label: '源分辨率' },
            ]}
          />
          <Select
            value={subtitleMode}
            onChange={setSubtitleMode}
            style={{ width: 190 }}
            options={[
              {
                value: 'burn_in',
                label: '烧录字幕',
                disabled: !hasTranscript,
              },
              {
                value: 'external',
                label: '独立字幕文件',
                disabled: !hasTranscript,
              },
              { value: 'none', label: '无字幕' },
            ]}
          />
          <Select
            value={aspectRatio}
            onChange={setAspectRatio}
            style={{ width: 120 }}
            options={[
              { value: '9:16', label: '竖屏 9:16' },
              { value: '16:9', label: '横屏 16:9' },
            ]}
          />
          <Select
            value={layoutMode}
            onChange={setLayoutMode}
            style={{ width: 150 }}
            options={[
              { value: 'blur_background', label: '模糊背景填充' },
              { value: 'smart_crop', label: '智能裁切' },
            ]}
          />
        </Space>

        <Space wrap size="middle" align="center">
          <Space size={6}>
            <Text type="secondary">帧率</Text>
            <Select
              value={fps === null ? 'source' : String(fps)}
              onChange={(v) => patchOptions({ fps: v === 'source' ? null : Number(v) })}
              style={{ width: 130 }}
              options={[
                { value: 'source', label: '跟随源' },
                ...fpsOptions.map((value) => ({ value: String(value), label: `${value} fps` })),
              ]}
            />
          </Space>
          <Tooltip title="数值越小画质越高、文件越大；预览不受此项影响，始终用快速档">
            <Space size={6}>
              <Text type="secondary">画质 CRF</Text>
              <Slider
                min={crfRange[0]}
                max={crfRange[1]}
                value={crf}
                onChange={(value) => patchOptions({ crf: value })}
                style={{ width: 140 }}
              />
              <Text style={{ display: 'inline-block', width: 24 }}>{crf}</Text>
            </Space>
          </Tooltip>
          {audioTracks.length > 1 && (
            <Space size={6}>
              <Text type="secondary">导出音轨</Text>
              <Select
                value={trackIndex}
                onChange={(value) => patchOptions({ audioTrackIndex: value })}
                style={{ width: 190 }}
                options={audioTracks.map((track, index) => ({
                  value: index,
                  label: `第 ${index + 1} 条 · ${track.codec_name}${
                    track.channels ? ` · ${track.channels}ch` : ''
                  }`,
                }))}
              />
            </Space>
          )}
        </Space>

        {outputPath && (
          <Text type="secondary" style={{ wordBreak: 'break-all' }}>
            输出到：{outputPath}
          </Text>
        )}
        {!isTauri() && (
          <Text type="secondary">浏览器模式下由后端决定导出位置（应用数据目录 exports）。</Text>
        )}
        {!hasTranscript && (
          <Text type="secondary" style={{ display: 'block' }}>
            本项目还没有语音识别结果，字幕选项已锁定为「无字幕」。打开上方「语音识别」重新分析一次，
            拿到转录后即可选择烧录或独立字幕。
          </Text>
        )}

        {notice && <Alert type="warning" showIcon message={notice} closable onClose={() => setNotice(null)} />}
        {running && (
          <Progress
            percent={Math.round((job?.progress ?? 0) * 100)}
            status="active"
            format={() => job?.message || '渲染中'}
          />
        )}
        {job && job.status === 'failed' && (
          <Alert type="error" showIcon message="导出失败" description={job.message} />
        )}
        {job && job.status === 'cancelled' && <Alert message="导出已取消" showIcon />}
        {result && (
          <Descriptions column={2} size="small" bordered>
            <Descriptions.Item label="文件" span={2}>
              <Text copyable style={{ wordBreak: 'break-all' }}>
                {result.output_path}
              </Text>
            </Descriptions.Item>
            <Descriptions.Item label="时长">
              {(result.duration_us / 1_000_000).toFixed(2)}s
            </Descriptions.Item>
            <Descriptions.Item label="大小">
              {((result.size_bytes ?? 0) / 1024 / 1024).toFixed(1)} MB
            </Descriptions.Item>
            <Descriptions.Item label="画幅">
              {result.width ?? '?'}×{result.height ?? '?'}
            </Descriptions.Item>
            <Descriptions.Item label="字幕">{result.subtitle_path ?? '无'}</Descriptions.Item>
          </Descriptions>
        )}

        <Space>
          <Button type="primary" loading={running} disabled={running} onClick={() => void handleExport()}>
            {running ? '导出中…' : '开始导出'}
          </Button>
          {running && (
            <Button icon={<StopOutlined />} onClick={() => void watch.cancel()}>
              取消
            </Button>
          )}
          {result && (
            <Button icon={<LinkOutlined />} onClick={() => void copyTicket()}>
              复制播放地址
            </Button>
          )}
          <Tooltip
            title={
              capabilities?.otio_available
                ? '给 Premiere / Resolve / Kdenlive 打开的剪辑工程，只含剪切点与媒体路径，不含画面'
                : '本项目的 OTIO 产物未通过官方读取器验证，暂不提供导出'
            }
          >
            <Button
              icon={<ExportOutlined />}
              loading={otioBusy}
              disabled={!capabilities?.otio_available}
              onClick={() => void handleExportOtio()}
            >
              导出 OTIO 工程
            </Button>
          </Tooltip>
        </Space>
        {otioPath && (
          <div style={{ wordBreak: 'break-all' }}>
            <Text type="secondary">OTIO 文件: </Text>
            <Text copyable>{otioPath}</Text>
            <Text type="secondary">
              {' '}
              · 换机器或移动过源素材后，外部编辑器会提示媒体离线，需要重连到新的文件路径
            </Text>
          </div>
        )}
      </Space>
    </Card>
  )
}

export default ExportDialog
