import { useCallback, useEffect, useMemo, useState } from 'react'
import { Alert, Button, Card, Empty, Input, Space, Spin, Tag, Typography, message } from 'antd'
import { RollbackOutlined, SaveOutlined } from '@ant-design/icons'
import { apiError } from '../api/backend'
import { pipelineApi } from '../api/client'
import type { Transcript } from '../api/types'

const { Text } = Typography

interface Props {
  projectId: string
  /** 分析完成后父级递增它：新转录落地时这个面板要重新拉取，否则一直停在空状态 */
  reloadToken?: number
  /** 每次写回后通知父级，好让"预览烧录字幕"等依赖转录状态的开关重新评估 */
  onChanged?: () => void
}

function seconds(us: number): string {
  return `${(us / 1_000_000).toFixed(2)}s`
}

function SubtitleTextEditor({ projectId, reloadToken = 0, onChanged }: Props) {
  const [transcript, setTranscript] = useState<Transcript | null>(null)
  const [drafts, setDrafts] = useState<Record<string, string>>({})
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const refresh = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      setTranscript(await pipelineApi.transcript(projectId))
      setDrafts({})
    } catch (err) {
      setError(apiError(err))
    } finally {
      setLoading(false)
    }
  }, [projectId])

  useEffect(() => {
    void refresh()
  }, [refresh, reloadToken])

  const dirtyKeys = useMemo(
    () =>
      (transcript?.utterances ?? [])
        .filter((u) => drafts[u.time_key] !== undefined && drafts[u.time_key] !== u.text)
        .map((u) => u.time_key),
    [transcript, drafts],
  )

  const save = async () => {
    if (!transcript || !dirtyKeys.length) return
    setSaving(true)
    try {
      const updated = await pipelineApi.setTranscriptOverrides(
        projectId,
        dirtyKeys.map((key) => ({ time_key: key, text: drafts[key] })),
      )
      setTranscript(updated)
      setDrafts({})
      message.success(`已更新 ${dirtyKeys.length} 句字幕文案`)
      onChanged?.()
    } catch (err) {
      message.error(apiError(err))
    } finally {
      setSaving(false)
    }
  }

  const revert = async (timeKey: string) => {
    setSaving(true)
    try {
      setTranscript(await pipelineApi.setTranscriptOverrides(projectId, [{ time_key: timeKey, text: '' }]))
      onChanged?.()
    } catch (err) {
      message.error(apiError(err))
    } finally {
      setSaving(false)
    }
  }

  const clearUnapplied = async () => {
    if (!transcript?.unapplied_overrides.length) return
    setSaving(true)
    try {
      setTranscript(
        await pipelineApi.setTranscriptOverrides(
          projectId,
          transcript.unapplied_overrides.map((o) => ({ time_key: o.time_key, text: '' })),
        ),
      )
      message.success('已清除未生效的文案')
    } catch (err) {
      message.error(apiError(err))
    } finally {
      setSaving(false)
    }
  }

  const body = (() => {
    if (loading) return <Spin style={{ display: 'block', margin: '24px auto' }} />
    if (error) return <Alert type="error" showIcon message={error} />
    if (!transcript || !transcript.available || !transcript.utterances.length) {
      return <Empty description="还没有语音识别结果，先完成一次分析才能编辑字幕" />
    }
    return (
      <>
        {transcript.unapplied_overrides.length > 0 && (
          <Alert
            type="warning"
            showIcon
            style={{ marginBottom: 12 }}
            message={`有 ${transcript.unapplied_overrides.length} 句改过的文案在当前转录里找不到对应时间区间（重新识别过就会这样），改动没有生效`}
            action={
              <Button size="small" onClick={() => void clearUnapplied()} disabled={saving}>
                清除
              </Button>
            }
          />
        )}
        <Space direction="vertical" size={8} style={{ width: '100%' }}>
          {transcript.utterances.map((utterance) => {
            const draft = drafts[utterance.time_key] ?? utterance.text
            const dirty = drafts[utterance.time_key] !== undefined && draft !== utterance.text
            return (
              <Space key={utterance.time_key} align="baseline" wrap style={{ width: '100%' }}>
                <Text type="secondary" style={{ fontVariantNumeric: 'tabular-nums' }}>
                  {seconds(utterance.start_us)} → {seconds(utterance.end_us)}
                </Text>
                <Input.TextArea
                  value={draft}
                  autoSize={{ minRows: 1, maxRows: 3 }}
                  style={{ width: 420 }}
                  onChange={(event) =>
                    setDrafts((prev) => ({ ...prev, [utterance.time_key]: event.target.value }))
                  }
                />
                {utterance.edited && !dirty && <Tag color="blue">已改写</Tag>}
                {dirty && <Tag color="orange">未保存</Tag>}
                {utterance.edited && (
                  <Button
                    size="small"
                    icon={<RollbackOutlined />}
                    disabled={saving}
                    onClick={() => void revert(utterance.time_key)}
                    title={`恢复为：${utterance.auto_text}`}
                  >
                    恢复自动
                  </Button>
                )}
              </Space>
            )
          })}
        </Space>
      </>
    )
  })()

  return (
    <Card
      title="字幕文案"
      extra={
        <Space>
          <Text type="secondary">{dirtyKeys.length ? `${dirtyKeys.length} 句待保存` : '改文字不影响时间轴'}</Text>
          <Button
            type="primary"
            size="small"
            icon={<SaveOutlined />}
            disabled={!dirtyKeys.length}
            loading={saving}
            onClick={() => void save()}
          >
            保存修改
          </Button>
        </Space>
      }
    >
      {body}
    </Card>
  )
}

export default SubtitleTextEditor
