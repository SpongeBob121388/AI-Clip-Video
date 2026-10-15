import { useState } from 'react'
import {
  Alert,
  Button,
  Card,
  ColorPicker,
  Divider,
  Form,
  InputNumber,
  Select,
  Slider,
  Space,
  Switch,
  Typography,
  message,
} from 'antd'
import { EyeOutlined } from '@ant-design/icons'
import { apiError } from '../api/backend'
import { pipelineApi } from '../api/client'
import type { SubtitleConfig, SubtitleStyle } from '../api/types'

const { Text } = Typography

export const defaultSubtitleConfig: SubtitleConfig = {
  format: 'ass',
  style: {
    font_name: 'Microsoft YaHei',
    font_size: 48,
    primary_color: '&H00FFFFFF',
    outline_color: '&H00000000',
    bold: false,
    italic: false,
    outline: 2,
    shadow: 1,
    alignment: 2,
    margin_l: 20,
    margin_r: 20,
    margin_v: 40,
  },
  safe_margin_horizontal: 0.1,
  safe_margin_vertical: 0.1,
  max_chars_per_line: 20,
}

/** ASS 的写法是 &H00BBGGRR，ColorPicker 说的是 #RRGGBB：字节序必须换。 */
export function assToHex(ass: string): string {
  const match = /^&H[0-9A-Fa-f]{2}([0-9A-Fa-f]{2})([0-9A-Fa-f]{2})([0-9A-Fa-f]{2})$/.exec(ass)
  if (!match) return '#FFFFFF'
  const [, bb, gg, rr] = match
  return `#${rr}${gg}${bb}`.toUpperCase()
}

export function hexToAss(hex: string): string {
  const clean = hex.replace('#', '')
  if (!/^[0-9A-Fa-f]{6}$/.test(clean)) return '&H00FFFFFF'
  const rr = clean.slice(0, 2)
  const gg = clean.slice(2, 4)
  const bb = clean.slice(4, 6)
  return `&H00${bb}${gg}${rr}`.toUpperCase()
}

interface Props {
  value: SubtitleConfig
  onChange: (config: SubtitleConfig) => void
  projectId?: string
  hasTranscript?: boolean
}

/** ASS 里是 &H00BBGGRR：#FFFF00（黄）写成 &H0000FFFF。 */
const COLOR_PRESETS: { label: string; primary: string; outline: string }[] = [
  { label: '白字黑边', primary: '&H00FFFFFF', outline: '&H00000000' },
  { label: '黄字白边', primary: '&H0000FFFF', outline: '&H00FFFFFF' },
  { label: '青字黑边', primary: '&H00FFFF00', outline: '&H00000000' },
  { label: '天蓝黑边', primary: '&H00EBCE87', outline: '&H00000000' },
]

function SubtitleConfigPanel({ value, onChange, projectId, hasTranscript }: Props) {
  const [preview, setPreview] = useState<string | null>(null)
  const [previewError, setPreviewError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)

  const patch = (partial: Partial<SubtitleConfig>) => onChange({ ...value, ...partial })
  const patchStyle = (partial: Partial<SubtitleStyle>) =>
    onChange({ ...value, style: { ...value.style, ...partial } })

  const renderPreview = async () => {
    if (!projectId) return
    setLoading(true)
    setPreviewError(null)
    try {
      const result = await pipelineApi.subtitles(projectId, value)
      setPreview(result.content)
      if (result.used_srt_fallback) {
        message.warning('该样式无法用 SRT 表达，预览已降级')
      }
    } catch (err) {
      setPreview(null)
      setPreviewError(apiError(err))
    } finally {
      setLoading(false)
    }
  }

  return (
    <Card title="字幕配置">
      <Form layout="vertical">
        <Space wrap size="large">
          <Form.Item label="格式">
            <Select
              value={value.format}
              onChange={(format) => patch({ format })}
              style={{ width: 120 }}
              options={[
                { value: 'ass', label: 'ASS（带样式）' },
                { value: 'srt', label: 'SRT（纯文本）' },
              ]}
            />
          </Form.Item>
          <Form.Item label="字体">
            <Select
              value={value.style.font_name}
              onChange={(font_name) => patchStyle({ font_name })}
              style={{ width: 160 }}
              options={[
                { value: 'Microsoft YaHei', label: '微软雅黑' },
                { value: 'SimHei', label: '黑体' },
                { value: 'SimSun', label: '宋体' },
                { value: 'Noto Sans SC', label: '思源黑体' },
                { value: 'Arial', label: 'Arial' },
              ]}
            />
          </Form.Item>
          <Form.Item label="字号">
            <InputNumber
              value={value.style.font_size}
              onChange={(v) => patchStyle({ font_size: v ?? 48 })}
              min={8}
              max={200}
            />
          </Form.Item>
          <Form.Item label="每行字数">
            <InputNumber
              value={value.max_chars_per_line}
              onChange={(v) => patch({ max_chars_per_line: v ?? 20 })}
              min={5}
              max={100}
            />
          </Form.Item>
          <Form.Item label="对齐">
            <Select
              value={value.style.alignment}
              onChange={(alignment) => patchStyle({ alignment })}
              style={{ width: 120 }}
              options={[
                { value: 1, label: '左下' },
                { value: 2, label: '中下' },
                { value: 3, label: '右下' },
                { value: 4, label: '左中' },
                { value: 5, label: '居中' },
                { value: 6, label: '右中' },
                { value: 7, label: '左上' },
                { value: 8, label: '中上' },
                { value: 9, label: '右上' },
              ]}
            />
          </Form.Item>
        </Space>

        <Divider style={{ margin: '4px 0 12px' }} />

        <Space wrap size="large">
          <Form.Item label="描边宽度">
            <Slider
              value={value.style.outline}
              onChange={(outline) => patchStyle({ outline })}
              min={0}
              max={10}
              step={0.5}
              style={{ width: 120 }}
            />
          </Form.Item>
          <Form.Item label="阴影宽度">
            <Slider
              value={value.style.shadow}
              onChange={(shadow) => patchStyle({ shadow })}
              min={0}
              max={10}
              step={0.5}
              style={{ width: 120 }}
            />
          </Form.Item>
          <Form.Item label="文字颜色">
            <ColorPicker
              value={assToHex(value.style.primary_color)}
              onChangeComplete={(color) =>
                patchStyle({ primary_color: hexToAss(color.toHexString()) })
              }
            />
          </Form.Item>
          <Form.Item label="描边颜色">
            <ColorPicker
              value={assToHex(value.style.outline_color)}
              onChangeComplete={(color) =>
                patchStyle({ outline_color: hexToAss(color.toHexString()) })
              }
            />
          </Form.Item>
          <Form.Item label="颜色预设">
            <Space size={4}>
              {COLOR_PRESETS.map((preset) => (
                <Button
                  key={preset.label}
                  size="small"
                  onClick={() =>
                    patchStyle({ primary_color: preset.primary, outline_color: preset.outline })
                  }
                >
                  {preset.label}
                </Button>
              ))}
            </Space>
          </Form.Item>
          <Space size={4}>
            <Switch
              size="small"
              checked={value.style.bold}
              onChange={(bold) => patchStyle({ bold })}
            />
            <Text type="secondary">粗体</Text>
          </Space>
          <Space size={4}>
            <Switch
              size="small"
              checked={value.style.italic}
              onChange={(italic) => patchStyle({ italic })}
            />
            <Text type="secondary">斜体</Text>
          </Space>
        </Space>

        <Divider style={{ margin: '4px 0 12px' }} />

        <Space wrap size="large">
          <Form.Item label="左边距(px)">
            <InputNumber
              value={value.style.margin_l}
              onChange={(v) => patchStyle({ margin_l: v ?? 20 })}
              min={0}
              max={500}
            />
          </Form.Item>
          <Form.Item label="右边距(px)">
            <InputNumber
              value={value.style.margin_r}
              onChange={(v) => patchStyle({ margin_r: v ?? 20 })}
              min={0}
              max={500}
            />
          </Form.Item>
          <Form.Item label="垂直边距(px)">
            <InputNumber
              value={value.style.margin_v}
              onChange={(v) => patchStyle({ margin_v: v ?? 40 })}
              min={0}
              max={500}
            />
          </Form.Item>
          <Form.Item label="横向安全区">
            <Slider
              value={value.safe_margin_horizontal}
              onChange={(v) => patch({ safe_margin_horizontal: v })}
              min={0}
              max={0.3}
              step={0.01}
              style={{ width: 120 }}
            />
          </Form.Item>
          <Form.Item label="纵向安全区">
            <Slider
              value={value.safe_margin_vertical}
              onChange={(v) => patch({ safe_margin_vertical: v })}
              min={0}
              max={0.3}
              step={0.01}
              style={{ width: 120 }}
            />
          </Form.Item>
        </Space>

        {projectId && (
          <Space wrap style={{ marginBottom: 8 }}>
            <Button
              size="small"
              icon={<EyeOutlined />}
              loading={loading}
              disabled={!hasTranscript}
              onClick={() => void renderPreview()}
            >
              生成字幕预览
            </Button>
            {!hasTranscript && (
              <Text type="secondary">需要语音识别结果才能生成字幕</Text>
            )}
          </Space>
        )}
        {previewError && (
          <Alert type="warning" showIcon closable message={previewError} onClose={() => setPreviewError(null)} />
        )}
        {preview && (
          <pre
            style={{
              maxHeight: 240,
              overflow: 'auto',
              background: '#fafafa',
              padding: 12,
              fontSize: 12,
              whiteSpace: 'pre-wrap',
            }}
          >
            {preview}
          </pre>
        )}
        <Text type="secondary">
          字体白名单外的名称会被自动替换为微软雅黑；SRT 不含样式，导出时只保留时间与文本。
        </Text>
      </Form>
    </Card>
  )
}

export default SubtitleConfigPanel
