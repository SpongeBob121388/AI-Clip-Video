import { useMemo, useState } from 'react'
import { Button, Card, InputNumber, List, Space, Tag, Typography, message } from 'antd'
import { CheckOutlined, CloseOutlined, PlusOutlined, RollbackOutlined, SwapOutlined } from '@ant-design/icons'
import type { Suggestion } from '../api/types'

const { Text } = Typography

type Decide = (ids: string[], status: 'accepted' | 'rejected' | 'pending') => Promise<boolean>

interface Props {
  suggestions: Suggestion[]
  loading: boolean
  expectedDurationUs: number
  sourceDurationUs: number
  onDecide: Decide
  onAddInterval: (startUs: number, endUs: number) => Promise<boolean>
}

const statusColor: Record<string, string> = {
  pending: 'orange',
  accepted: 'red',
  rejected: 'default',
  modified: 'blue',
}

const statusLabels: Record<string, string> = {
  pending: '待定',
  accepted: '删除',
  rejected: '保留',
  modified: '已改',
}

/** suggestion_type 的中文标签；未知类型直接显示原值，避免出现"（未知）"这种没信息量的字 */
const typeLabels: Record<string, string> = {
  silence: '静音',
  filler_word: '口头禅',
  repetition: '重复',
  pause: '停顿',
  manual: '手工',
}

function formatSeconds(us: number): string {
  return `${(us / 1_000_000).toFixed(2)}s`
}

function SuggestionReview({
  suggestions,
  loading,
  expectedDurationUs,
  sourceDurationUs,
  onDecide,
  onAddInterval,
}: Props) {
  const [startSec, setStartSec] = useState<number | null>(null)
  const [endSec, setEndSec] = useState<number | null>(null)

  const pendingIds = useMemo(
    () => suggestions.filter((s) => s.status === 'pending').map((s) => s.id),
    [suggestions],
  )
  // 反转只针对"已经表过态"的建议，未决项没有可反向的决策
  const decided = useMemo(
    () => suggestions.filter((s) => s.status !== 'pending'),
    [suggestions],
  )

  const run = async (ids: string[], status: 'accepted' | 'rejected' | 'pending') => {
    if (!ids.length) {
      message.info('没有需要处理的建议')
      return
    }
    // 一次请求提交全部 ID：后端在一个修订里改完，撤销时一步回到批量操作之前
    const ok = await onDecide(ids, status)
    if (ok) {
      const verb = status === 'accepted' ? '接受' : status === 'rejected' ? '拒绝' : '退回待定'
      message.success(`已${verb} ${ids.length} 条建议`)
    }
  }

  const handleInvert = async () => {
    const accept = decided.filter((s) => s.status === 'rejected').map((s) => s.id)
    const reject = decided.filter((s) => s.status === 'accepted').map((s) => s.id)
    if (accept.length) await run(accept, 'accepted')
    if (reject.length) await run(reject, 'rejected')
  }

  const handleAdd = async () => {
    if (startSec === null || endSec === null) {
      message.warning('请填写起止时间')
      return
    }
    const startUs = Math.round(startSec * 1_000_000)
    const endUs = Math.round(endSec * 1_000_000)
    if (endUs <= startUs) {
      message.warning('结束时间必须晚于开始时间')
      return
    }
    const ok = await onAddInterval(startUs, endUs)
    if (ok) message.success('已添加剪切区间')
  }

  return (
    <Card
      title={`剪辑建议（${suggestions.length}）`}
      extra={
        <Space wrap>
          <Text type="secondary">
            输出 {formatSeconds(expectedDurationUs)} / 源 {formatSeconds(sourceDurationUs)}
          </Text>
          <Button
            size="small"
            icon={<CheckOutlined />}
            disabled={loading || pendingIds.length === 0}
            onClick={() => void run(pendingIds, 'accepted')}
          >
            全部接受
          </Button>
          <Button
            size="small"
            icon={<CloseOutlined />}
            disabled={loading || pendingIds.length === 0}
            onClick={() => void run(pendingIds, 'rejected')}
          >
            全部拒绝
          </Button>
          <Button
            size="small"
            icon={<SwapOutlined />}
            disabled={loading || decided.length === 0}
            onClick={() => void handleInvert()}
          >
            反转
          </Button>
        </Space>
      }
    >
      <Space style={{ marginBottom: 12 }} wrap>
        <InputNumber
          placeholder="起点(秒)"
          min={0}
          step={0.1}
          value={startSec}
          onChange={setStartSec}
          style={{ width: 120 }}
        />
        <InputNumber
          placeholder="终点(秒)"
          min={0}
          step={0.1}
          value={endSec}
          onChange={setEndSec}
          style={{ width: 120 }}
        />
        <Button size="small" icon={<PlusOutlined />} disabled={loading} onClick={() => void handleAdd()}>
          手动添加剪切区间
        </Button>
      </Space>

      <List
        dataSource={suggestions}
        locale={{ emptyText: '暂无剪辑建议，点击「开始分析」生成' }}
        renderItem={(s) => (
          <List.Item
            actions={[
              <Button
                key="accept"
                size="small"
                type={s.status === 'accepted' ? 'default' : 'primary'}
                icon={<CheckOutlined />}
                disabled={loading}
                onClick={() => void run([s.id], 'accepted')}
              >
                删除此段
              </Button>,
              <Button
                key="reject"
                size="small"
                disabled={loading}
                icon={<CloseOutlined />}
                onClick={() => void run([s.id], 'rejected')}
              >
                保留
              </Button>,
              ...(s.status !== 'pending'
                ? [
                    <Button
                      key="pending"
                      size="small"
                      type="text"
                      disabled={loading}
                      icon={<RollbackOutlined />}
                      onClick={() => void run([s.id], 'pending')}
                    >
                      待定
                    </Button>,
                  ]
                : []),
            ]}
          >
            <List.Item.Meta
              title={
                <Space>
                  <Tag color={statusColor[s.status] ?? 'default'}>
                    {statusLabels[s.status] ?? s.status}
                  </Tag>
                  <Text>{formatSeconds(s.source_end_us - s.source_start_us)}</Text>
                  <Text type="secondary">
                    {formatSeconds(s.source_start_us)} → {formatSeconds(s.source_end_us)}
                  </Text>
                  <Tag>{typeLabels[s.suggestion_type] ?? s.suggestion_type}</Tag>
                </Space>
              }
              description={
                <Space direction="vertical" size={0}>
                  <Text>{s.reason || '（无说明）'}</Text>
                  {(s.prev_text || s.next_text) && (
                    <Text type="secondary">
                      …{s.prev_text} ▏{s.next_text}…
                    </Text>
                  )}
                </Space>
              }
            />
          </List.Item>
        )}
      />
    </Card>
  )
}

export default SuggestionReview
