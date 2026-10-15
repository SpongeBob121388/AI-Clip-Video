import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { PointerEvent as ReactPointerEvent, RefObject } from 'react'
import { Button, Card, Empty, Select, Space, Tag, Tooltip, Typography, message } from 'antd'
import type { OutputInterval, Suggestion } from '../api/types'

const { Text } = Typography

const LANE_HEIGHT = 46
const RULER_HEIGHT = 26
const OUTPUT_HEIGHT = 26
const HANDLE_PX = 7
const SNAP_PX = 8
const DRAG_THRESHOLD_PX = 3

const ZOOM_OPTIONS = [10, 20, 40, 80, 160, 400].map((px) => ({ value: px, label: `${px} px/秒` }))
const TICK_STEPS = [0.1, 0.2, 0.25, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 1800]

const BLOCK_STYLE: Record<string, { fill: string; border: string }> = {
  accepted: { fill: 'rgba(255,77,79,0.34)', border: '#cf1322' },
  pending: { fill: 'rgba(250,173,20,0.34)', border: '#ad6800' },
  rejected: { fill: 'rgba(140,140,140,0.16)', border: '#8c8c8c' },
  modified: { fill: 'rgba(24,144,255,0.30)', border: '#0958d9' },
}

const STATUS_LABEL: Record<string, string> = {
  accepted: '删除',
  pending: '待定',
  rejected: '保留',
  modified: '已改',
}

function fmt(us: number): string {
  const s = us / 1_000_000
  if (Math.abs(s) >= 60) {
    const m = Math.floor(Math.abs(s) / 60)
    return `${s < 0 ? '-' : ''}${m}:${(Math.abs(s) - m * 60).toFixed(2).padStart(5, '0')}`
  }
  return `${s.toFixed(2)}s`
}

/** 指针捕获只是"拖出容器也继续跟手"的优化，失败时不能把整个拖拽打断。 */
function setCapture(target: EventTarget | null, pointerId: number, release = false): void {
  const el = target as HTMLElement | null
  if (!el) return
  try {
    if (release) el.releasePointerCapture?.(pointerId)
    else el.setPointerCapture?.(pointerId)
  } catch {
    /* 合成事件或指针已抬起时浏览器会拒绝捕获，忽略即可 */
  }
}

type DragKind = 'move' | 'trim-start' | 'trim-end' | 'create' | 'scrub'

interface DragState {
  kind: DragKind
  suggestionId?: string
  index: number
  grabUs: number
  grabClientX: number
  startUs: number
  endUs: number
  moved: boolean
}

interface Draft {
  suggestionId?: string
  startUs: number
  endUs: number
}

interface Props {
  durationUs: number
  suggestions: Suggestion[]
  outputIntervals: OutputInterval[]
  expectedDurationUs: number
  fps: number | null
  loading: boolean
  videoRef: RefObject<HTMLVideoElement | null>
  onAdd: (startUs: number, endUs: number) => Promise<boolean>
  onModify: (oldStartUs: number, startUs: number, endUs: number) => Promise<boolean>
  onRemove: (startUs: number, endUs: number) => Promise<boolean>
  onMerge: (firstStartUs: number, secondEndUs: number) => Promise<boolean>
  onSplit: (startUs: number, splitAtUs: number) => Promise<boolean>
  onDecide: (ids: string[], status: 'accepted' | 'rejected' | 'pending') => Promise<boolean>
}

function TimelineEditor({
  durationUs,
  suggestions,
  outputIntervals,
  expectedDurationUs,
  fps,
  loading,
  videoRef,
  onAdd,
  onModify,
  onRemove,
  onMerge,
  onSplit,
  onDecide,
}: Props) {
  const [pxPerSec, setPxPerSec] = useState(40)
  const [selected, setSelected] = useState<string[]>([])
  const [draft, setDraft] = useState<Draft | null>(null)
  const [playheadLabel, setPlayheadLabel] = useState(0)
  const contentRef = useRef<HTMLDivElement | null>(null)
  const playheadRef = useRef<HTMLDivElement | null>(null)
  const playheadUs = useRef(0)
  const drag = useRef<DragState | null>(null)

  const frameUs = fps && fps > 0 ? Math.max(1, Math.round(1_000_000 / fps)) : 10_000
  const sorted = useMemo(
    () => [...suggestions].sort((a, b) => a.source_start_us - b.source_start_us),
    [suggestions],
  )
  const widthPx = Math.max(320, Math.ceil((durationUs / 1_000_000) * pxPerSec))

  // 选中的块被合并/拆分掉之后，选择集要跟着失效，否则按钮会对着已经不存在的区间发操作
  useEffect(() => {
    setSelected((ids) => ids.filter((id) => suggestions.some((s) => s.id === id)))
  }, [suggestions])

  const paintPlayhead = useCallback((us: number) => {
    if (playheadRef.current) playheadRef.current.style.transform = `translateX(${(us / 1e6) * pxPerSec}px)`
  }, [pxPerSec])

  // 播放头用 rAF 直接改 transform：放进 state 会让整条时间轴每秒重绘 60 次
  useEffect(() => {
    let cancelled = false
    const tick = () => {
      const video = videoRef.current
      if (cancelled) return
      if (video && !video.paused && durationUs > 0) {
        const us = Math.min(durationUs, Math.round(video.currentTime * 1_000_000))
        playheadUs.current = us
        paintPlayhead(us)
        setPlayheadLabel((prev) => (Math.abs(prev - us) > 100_000 ? us : prev))
      }
      requestAnimationFrame(tick)
    }
    requestAnimationFrame(tick)
    return () => {
      cancelled = true
    }
  }, [videoRef, durationUs, paintPlayhead])

  const clampUs = useCallback((us: number) => Math.max(0, Math.min(durationUs, us)), [durationUs])

  const timeFromClientX = useCallback(
    (clientX: number) => {
      const el = contentRef.current
      if (!el || durationUs <= 0) return 0
      const rect = el.getBoundingClientRect()
      return clampUs(((clientX - rect.left) / pxPerSec) * 1_000_000)
    },
    [clampUs, durationUs, pxPerSec],
  )

  const snap = useCallback(
    (us: number, targets: number[], minUs: number, maxUs: number) => {
      let value = Math.round(us / frameUs) * frameUs
      const tolerance = (SNAP_PX / pxPerSec) * 1_000_000
      let best = tolerance
      for (const target of targets) {
        const distance = Math.abs(target - us)
        if (distance <= best) {
          best = distance
          value = target
        }
      }
      return Math.max(minUs, Math.min(maxUs, Math.round(value)))
    },
    [frameUs, pxPerSec],
  )

  /** 只取整到帧、不做邻边吸附：给"按下那一刻"的锚点用。 */
  const frameAlign = useCallback((us: number) => Math.round(us / frameUs) * frameUs, [frameUs])

  const seek = useCallback(
    (us: number) => {
      const video = videoRef.current
      playheadUs.current = us
      setPlayheadLabel(us)
      paintPlayhead(us)
      if (video) video.currentTime = us / 1_000_000
    },
    [paintPlayhead, videoRef],
  )

  const begin = (event: ReactPointerEvent, state: DragState) => {
    if (loading || durationUs <= 0) return
    setCapture(event.currentTarget, event.pointerId)
    drag.current = state
    if (state.kind === 'scrub') {
      seek(clampUs(state.grabUs))
    } else if (state.suggestionId) {
      setDraft({ suggestionId: state.suggestionId, startUs: state.startUs, endUs: state.endUs })
    } else {
      setDraft({ startUs: state.grabUs, endUs: state.grabUs })
    }
  }

  const boundsFor = (index: number, ignoreId?: string) => {
    const others = sorted.filter((s) => s.id !== ignoreId)
    const edges = others.flatMap((s) => [s.source_start_us, s.source_end_us])
    let prev: Suggestion | undefined
    for (let i = index - 1; i >= 0; i -= 1) {
      if (sorted[i].id !== ignoreId) {
        prev = sorted[i]
        break
      }
    }
    let next: Suggestion | undefined
    for (let i = index + 1; i < sorted.length; i += 1) {
      if (sorted[i].id !== ignoreId) {
        next = sorted[i]
        break
      }
    }
    return {
      targets: [0, durationUs, ...edges],
      leftLimit: prev ? prev.source_end_us : 0,
      rightLimit: next ? next.source_start_us : durationUs,
    }
  }

  const onPointerMove = (event: ReactPointerEvent) => {
    const d = drag.current
    if (!d) return
    if (!d.moved && Math.abs(event.clientX - d.grabClientX) > DRAG_THRESHOLD_PX) d.moved = true
    const us = timeFromClientX(event.clientX)
    if (d.kind === 'scrub') {
      seek(clampUs(us))
      return
    }
    if (!d.moved) return
    if (d.kind === 'create') {
      // anchor 不能进 targets：否则离 anchor 最近的那条边永远"吸附到自己"，
      // 起始边就会保留原始指针时间，落在整帧之外（真鼠标走查实测到过 350000us @25fps）
      const a = snap(Math.min(d.grabUs, us), [0, durationUs], 0, durationUs)
      const b = snap(Math.max(d.grabUs, us), [0, durationUs], 0, durationUs)
      setDraft({ startUs: Math.min(a, b), endUs: Math.max(a, b) })
      return
    }
    const { targets, leftLimit, rightLimit } = boundsFor(d.index, d.suggestionId)
    const length = d.endUs - d.startUs
    if (d.kind === 'move') {
      const start = snap(d.startUs + (us - d.grabUs), targets, leftLimit, Math.max(leftLimit, rightLimit - length))
      setDraft({ suggestionId: d.suggestionId, startUs: start, endUs: start + length })
      return
    }
    if (d.kind === 'trim-start') {
      const start = snap(us, targets, leftLimit, d.endUs - frameUs)
      setDraft({ suggestionId: d.suggestionId, startUs: Math.min(start, d.endUs - frameUs), endUs: d.endUs })
      return
    }
    const end = snap(us, targets, d.startUs + frameUs, rightLimit)
    setDraft({ suggestionId: d.suggestionId, startUs: d.startUs, endUs: Math.max(end, d.startUs + frameUs) })
  }

  const toggleSelect = (id?: string) => {
    if (!id) return
    setSelected((prev) => (prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]))
  }

  const onPointerUp = async (event: ReactPointerEvent) => {
    const d = drag.current
    drag.current = null
    const current = draft
    setDraft(null)
    if (!d) return
    setCapture(event.currentTarget, event.pointerId, true)
    if (d.kind === 'scrub') return
    if (!d.moved) {
      if (d.kind !== 'create') toggleSelect(d.suggestionId)
      return
    }
    if (!current) return
    const { startUs, endUs } = current
    if (endUs - startUs < frameUs) {
      message.warning(`区间太短：至少一帧（${fmt(frameUs)}）`)
      return
    }
    if (d.kind === 'create') {
      if (await onAdd(startUs, endUs)) setSelected([])
      return
    }
    if (d.suggestionId && (await onModify(d.startUs, startUs, endUs))) {
      setSelected([d.suggestionId])
    }
  }

  const selectedBlocks = sorted.filter((s) => selected.includes(s.id))
  const canMerge = selectedBlocks.length >= 2
  const mergeSpan = canMerge
    ? {
        start: selectedBlocks[0].source_start_us,
        end: selectedBlocks[selectedBlocks.length - 1].source_end_us,
        // 后端会把首末之间"完全包含"的标记一起合并，按钮上要说清楚，别让用户以为只动了选中的那两个
        swallowed: sorted.filter(
          (s) => s.source_start_us >= selectedBlocks[0].source_start_us && s.source_end_us <= selectedBlocks[selectedBlocks.length - 1].source_end_us,
        ).length,
      }
    : null
  const single = selectedBlocks.length === 1 ? selectedBlocks[0] : null
  const head = playheadUs.current
  const canSplit = !!single && head > single.source_start_us + frameUs && head < single.source_end_us - frameUs

  const runAction = async (fn: () => Promise<boolean>, okText: string) => {
    if (await fn()) message.success(okText)
  }

  const ticks = useMemo(() => {
    if (durationUs <= 0) return []
    const step = TICK_STEPS.find((s) => s * pxPerSec >= 70) ?? 1800
    const out: number[] = []
    for (let sec = 0; sec * 1e6 <= durationUs + 1; sec += step) out.push(sec * 1_000_000)
    return out
  }, [durationUs, pxPerSec])

  const blockBox = (s: Suggestion) => {
    const isDraft = draft?.suggestionId === s.id
    const startUs = isDraft ? draft!.startUs : s.source_start_us
    const endUs = isDraft ? draft!.endUs : s.source_end_us
    const style = BLOCK_STYLE[s.status] ?? BLOCK_STYLE.pending
    const index = sorted.findIndex((x) => x.id === s.id)
    const common = {
      onPointerMove,
      onPointerUp,
      onPointerCancel: onPointerUp,
    }
    return (
      <div
        key={s.id}
        {...common}
        onPointerDown={(event) => {
          event.stopPropagation()
          begin(event, {
            kind: 'move',
            suggestionId: s.id,
            index,
            grabUs: timeFromClientX(event.clientX),
            grabClientX: event.clientX,
            startUs: s.source_start_us,
            endUs: s.source_end_us,
            moved: false,
          })
        }}
        style={{
          position: 'absolute',
          top: 4,
          height: LANE_HEIGHT - 8,
          left: (startUs / 1e6) * pxPerSec,
          width: Math.max(2, ((endUs - startUs) / 1e6) * pxPerSec),
          background: style.fill,
          border: `1px solid ${style.border}`,
          borderLeft: `2px solid ${style.border}`,
          borderRight: `2px solid ${style.border}`,
          borderRadius: 3,
          cursor: loading ? 'progress' : 'grab',
          outline: selected.includes(s.id) ? '2px solid #1677ff' : 'none',
          outlineOffset: 1,
          touchAction: 'none',
          overflow: 'hidden',
        }}
        title={`${fmt(s.source_start_us)} → ${fmt(s.source_end_us)} · ${STATUS_LABEL[s.status] ?? s.status}`}
      >
        <span style={{ fontSize: 11, lineHeight: '14px', display: 'block', padding: '1px 4px', whiteSpace: 'nowrap' }}>
          {STATUS_LABEL[s.status] ?? s.status}
        </span>
        <div
          {...common}
          onPointerDown={(event) => {
            event.stopPropagation()
            begin(event, {
              kind: 'trim-start',
              suggestionId: s.id,
              index,
              grabUs: timeFromClientX(event.clientX),
              grabClientX: event.clientX,
              startUs: s.source_start_us,
              endUs: s.source_end_us,
              moved: false,
            })
          }}
          style={{
            position: 'absolute',
            left: 0,
            top: 0,
            width: HANDLE_PX,
            height: '100%',
            cursor: 'ew-resize',
            background: 'rgba(0,0,0,0.28)',
            touchAction: 'none',
          }}
        />
        <div
          {...common}
          onPointerDown={(event) => {
            event.stopPropagation()
            begin(event, {
              kind: 'trim-end',
              suggestionId: s.id,
              index,
              grabUs: timeFromClientX(event.clientX),
              grabClientX: event.clientX,
              startUs: s.source_start_us,
              endUs: s.source_end_us,
              moved: false,
            })
          }}
          style={{
            position: 'absolute',
            right: 0,
            top: 0,
            width: HANDLE_PX,
            height: '100%',
            cursor: 'ew-resize',
            background: 'rgba(0,0,0,0.28)',
            touchAction: 'none',
          }}
        />
      </div>
    )
  }

  return (
    <Card
      title="时间轴"
      extra={
        <Space wrap>
          <Text type="secondary">
            输出 {fmt(expectedDurationUs)} / 源 {fmt(durationUs)}
          </Text>
          <Tooltip title="每秒占多少像素，越大越容易精确到帧">
            <Select
              size="small"
              value={pxPerSec}
              onChange={setPxPerSec}
              options={ZOOM_OPTIONS}
              style={{ width: 110 }}
            />
          </Tooltip>
        </Space>
      }
    >
      {durationUs <= 0 ? (
        <Empty description="还没有可用的剪辑记录，先绑定媒体并分析" />
      ) : (
        <>
          <Space wrap style={{ marginBottom: 10 }}>
            <Button
              size="small"
              type="primary"
              danger
              disabled={loading || !selectedBlocks.length}
              onClick={() => void runAction(() => onDecide(selected, 'accepted'), '已标记为删除')}
            >
              删除所选
            </Button>
            <Button
              size="small"
              disabled={loading || !selectedBlocks.length}
              onClick={() => void runAction(() => onDecide(selected, 'rejected'), '已保留所选')}
            >
              保留所选
            </Button>
            <Button
              size="small"
              disabled={loading || !selectedBlocks.length}
              onClick={() => void runAction(() => onDecide(selected, 'pending'), '已恢复为待定')}
            >
              待定
            </Button>
            <Tooltip title="删掉这个标记本身（不改变其它区间的决定）">
              <Button
                size="small"
                disabled={!single}
                onClick={() =>
                  void runAction(
                    () => onRemove(single!.source_start_us, single!.source_end_us),
                    '已移除标记',
                  )
                }
              >
                移除标记
              </Button>
            </Tooltip>
            <Button
              size="small"
              disabled={!canMerge}
              onClick={() =>
                void runAction(
                  () => onMerge(mergeSpan!.start, mergeSpan!.end),
                  `已合并 ${mergeSpan!.swallowed} 段`,
                )
              }
            >
              合并所选{mergeSpan && mergeSpan.swallowed > selectedBlocks.length ? `（含中间 ${mergeSpan.swallowed} 段）` : ''}
            </Button>
            <Tooltip title="在播放头位置把选中的一个区间拆成两段">
              <Button
                size="small"
                disabled={!canSplit}
                onClick={() =>
                  void runAction(() => onSplit(single!.source_start_us, head), '已在播放头拆分')
                }
              >
                播放头处拆分
              </Button>
            </Tooltip>
            <Button size="small" disabled={!selected.length} onClick={() => setSelected([])}>
              清空选择
            </Button>
            <Text type="secondary">播放头 {fmt(playheadLabel)}</Text>
          </Space>

          <div style={{ overflowX: 'auto', paddingBottom: 4 }}>
            <div
              ref={contentRef}
              tabIndex={0}
              style={{ position: 'relative', width: widthPx, userSelect: 'none', outline: 'none' }}
              onKeyDown={(event) => {
                if (event.key === 'Escape') setSelected([])
                if ((event.key === 'Delete' || event.key === 'Backspace') && single) {
                  event.preventDefault()
                  void runAction(() => onRemove(single.source_start_us, single.source_end_us), '已移除标记')
                }
              }}
            >
              <div
                onPointerMove={onPointerMove}
                onPointerUp={onPointerUp}
                onPointerCancel={onPointerUp}
                onPointerDown={(event) =>
                  begin(event, {
                    kind: 'scrub',
                    index: -1,
                    grabUs: timeFromClientX(event.clientX),
                    grabClientX: event.clientX,
                    startUs: 0,
                    endUs: 0,
                    moved: false,
                  })
                }
                style={{
                  position: 'relative',
                  height: RULER_HEIGHT,
                  borderBottom: '1px solid #d9d9d9',
                  cursor: 'text',
                  touchAction: 'none',
                }}
              >
                {ticks.map((us) => (
                  <div key={us} style={{ position: 'absolute', left: (us / 1e6) * pxPerSec, top: 0, height: '100%' }}>
                    <div style={{ position: 'absolute', bottom: 0, width: 1, height: 6, background: '#bfbfbf' }} />
                    <span style={{ fontSize: 10, color: '#8c8c8c', whiteSpace: 'nowrap', paddingLeft: 2 }}>
                      {fmt(us)}
                    </span>
                  </div>
                ))}
              </div>

              <div
                onPointerMove={onPointerMove}
                onPointerUp={onPointerUp}
                onPointerCancel={onPointerUp}
                onPointerDown={(event) =>
                  begin(event, {
                    kind: 'create',
                    index: sorted.length,
                    grabUs: frameAlign(timeFromClientX(event.clientX)),
                    grabClientX: event.clientX,
                    startUs: 0,
                    endUs: 0,
                    moved: false,
                  })
                }
                style={{
                  position: 'relative',
                  height: LANE_HEIGHT,
                  background: '#fafafa',
                  border: '1px dashed #d9d9d9',
                  borderRadius: 4,
                  cursor: 'crosshair',
                  touchAction: 'none',
                }}
              >
                {sorted.map(blockBox)}
                {draft && !draft.suggestionId && (
                  <div
                    style={{
                      position: 'absolute',
                      top: 4,
                      height: LANE_HEIGHT - 8,
                      left: (draft.startUs / 1e6) * pxPerSec,
                      width: Math.max(2, ((draft.endUs - draft.startUs) / 1e6) * pxPerSec),
                      background: 'rgba(22,119,255,0.22)',
                      border: '1px solid #1677ff',
                      borderRadius: 3,
                      pointerEvents: 'none',
                    }}
                  />
                )}
              </div>

              <Tooltip title="导出后真正保留下来的片段（按源时间对齐）">
                <div style={{ position: 'relative', height: OUTPUT_HEIGHT, marginTop: 6, background: '#f5f5f5', borderRadius: 3 }}>
                  {outputIntervals.map((interval) => {
                    const spanUs = interval.end_us - interval.start_us
                    return (
                      <div
                        key={`${interval.source_start_us}-${interval.start_us}`}
                        style={{
                          position: 'absolute',
                          top: 2,
                          height: OUTPUT_HEIGHT - 4,
                          left: (interval.source_start_us / 1e6) * pxPerSec,
                          width: Math.max(1, ((spanUs - 1) / 1e6) * pxPerSec),
                          background: 'rgba(82,196,26,0.45)',
                          border: '1px solid #389e0d',
                          borderRadius: 2,
                        }}
                        title={`保留 ${fmt(spanUs)}（源 ${fmt(interval.source_start_us)} 起）`}
                      />
                    )
                  })}
                </div>
              </Tooltip>

              <div
                ref={playheadRef}
                style={{
                  position: 'absolute',
                  top: 0,
                  left: 0,
                  height: RULER_HEIGHT + LANE_HEIGHT + OUTPUT_HEIGHT + 12,
                  width: 2,
                  background: '#1677ff',
                  pointerEvents: 'none',
                }}
              />
            </div>
          </div>

          <Space style={{ marginTop: 8 }} wrap size={4}>
            <Text type="secondary">在空白处拖出新区间</Text>
            <Text type="secondary">·</Text>
            <Text type="secondary">拖动块体挪位置，拖左右边缘改起止</Text>
            <Text type="secondary">·</Text>
            <Text type="secondary">点击块体选中/取消（可多选），选中后可删除/保留/合并/拆分</Text>
            <Text type="secondary">·</Text>
            {(['accepted', 'pending', 'rejected'] as const).map((status) => (
              <Tag key={status} color={status === 'accepted' ? 'red' : status === 'pending' ? 'orange' : 'default'}>
                {STATUS_LABEL[status]}
              </Tag>
            ))}
            <Text type="secondary">吸附到整帧与相邻边界；只在松手时提交一次修订</Text>
          </Space>
        </>
      )}
    </Card>
  )
}

export default TimelineEditor
