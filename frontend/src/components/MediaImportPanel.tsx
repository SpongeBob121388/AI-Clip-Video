import { useState } from 'react'
import { Alert, Button, Card, Descriptions, Progress, Space, Typography, message } from 'antd'
import { FolderOpenOutlined } from '@ant-design/icons'
import { apiError, isTauri } from '../api/backend'
import { mediaApi, projectsApi } from '../api/client'
import type { MediaProbe } from '../api/types'
import { useProjectStore } from '../stores/projectStore'

const { Text } = Typography

async function tauriOpenFile(): Promise<string | null> {
  const { open } = await import('@tauri-apps/plugin-dialog')
  const result = await open({
    multiple: false,
    filters: [
      {
        name: '媒体文件',
        extensions: ['mp4', 'mov', 'mkv', 'avi', 'webm', 'mp3', 'wav', 'm4a'],
      },
    ],
  })
  return typeof result === 'string' ? result : null
}

function MediaImportPanel({
  projectId,
  onProbe,
}: {
  projectId: string
  /** 探测结果交给父级：音轨选择、导出参数都要用同一份轨道信息 */
  onProbe?: (probe: MediaProbe) => void
}) {
  const [probe, setProbe] = useState<MediaProbe | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [uploadPercent, setUploadPercent] = useState<number | null>(null)
  const [filePath, setFilePath] = useState<string | null>(null)
  const native = isTauri()
  const setCurrentProject = useProjectStore((s) => s.setCurrentProject)

  const bind = async (path: string, result: MediaProbe) => {
    setFilePath(path)
    setProbe(result)
    onProbe?.(result)
    // 绑定到项目：后端据此校验源文件并建立基线剪辑版本，之后所有链路都走服务端路径
    const project = await projectsApi.attachSource(projectId, path)
    setCurrentProject(project)
  }

  const handleNativePick = async () => {
    setError(null)
    setLoading(true)
    try {
      const path = await tauriOpenFile()
      if (path) await bind(path, await mediaApi.probe(path))
    } catch (err) {
      setError(apiError(err))
    } finally {
      setLoading(false)
    }
  }

  const handleFileChange = async (event: React.ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0]
    event.target.value = ''
    if (!file) return
    setError(null)
    setLoading(true)
    setUploadPercent(0)
    try {
      const res = await mediaApi.upload(file, setUploadPercent)
      await bind(res.file_path, res.probe)
      message.success('媒体已上传并绑定到项目')
    } catch (err) {
      setError(apiError(err))
      setProbe(null)
    } finally {
      setLoading(false)
      setUploadPercent(null)
    }
  }

  const audioTracks = (probe?.tracks ?? []).filter((t) => t.codec_type === 'audio')

  return (
    <Card title="媒体导入" loading={loading}>
      {error && (
        <Alert
          type="error"
          message={error}
          closable
          onClose={() => setError(null)}
          style={{ marginBottom: 12 }}
        />
      )}
      <Space wrap style={{ marginBottom: 16 }} align="center">
        {native ? (
          <Button icon={<FolderOpenOutlined />} onClick={() => void handleNativePick()}>
            选择文件
          </Button>
        ) : (
          // 浏览器里没有原生对话框：直接暴露文件控件，上传到后端后再绑定项目
          <>
            <Text type="secondary">选择媒体文件：</Text>
            <input
              type="file"
              accept="video/*,audio/*"
              onChange={(event) => void handleFileChange(event)}
            />
          </>
        )}
      </Space>
      {uploadPercent !== null && <Progress percent={uploadPercent} size="small" />}
      {filePath && (
        <div style={{ marginBottom: 8, color: '#666', wordBreak: 'break-all' }}>文件: {filePath}</div>
      )}
      {probe && (
        <>
          <Descriptions column={2} size="small">
            <Descriptions.Item label="时长">
              {(probe.duration_us / 1_000_000).toFixed(1)}s
            </Descriptions.Item>
            <Descriptions.Item label="分辨率">
              {probe.width ?? '?'}×{probe.height ?? '?'}
            </Descriptions.Item>
            <Descriptions.Item label="帧率">{probe.fps ? `${probe.fps.toFixed(2)}fps` : '-'}</Descriptions.Item>
            <Descriptions.Item label="编码">
              {probe.codec_video ?? probe.codec_audio ?? 'unknown'}
            </Descriptions.Item>
            <Descriptions.Item label="音轨数">{audioTracks.length}</Descriptions.Item>
            <Descriptions.Item label="可变帧率">{probe.is_vfr ? '是（可能有时间偏移）' : '否'}</Descriptions.Item>
          </Descriptions>
          {audioTracks.length > 1 && (
            <Alert
              type="info"
              showIcon
              style={{ marginTop: 8 }}
              message={`检测到 ${audioTracks.length} 条音轨：在上方「音轨」选择识别与导出用哪一条`}
            />
          )}
          {audioTracks.length === 0 && (
            <Alert
              type="warning"
              showIcon
              style={{ marginTop: 8 }}
              message="该文件没有音轨，静音检测与语音识别将无法执行"
            />
          )}
        </>
      )}
    </Card>
  )
}

export default MediaImportPanel
