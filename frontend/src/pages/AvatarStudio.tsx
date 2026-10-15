import { useEffect, useState } from 'react'
import { Alert, Button, Card, Input, Select, Space, Typography, message } from 'antd'
import { apiError } from '../api/backend'
import { avatarApi } from '../api/client'

const { TextArea } = Input

interface AvatarCapabilities {
  available: boolean
  message: string
  providers: { id: string; available: boolean }[]
}

function AvatarStudio() {
  const [script, setScript] = useState('')
  const [provider, setProvider] = useState('heygen')
  const [speed, setSpeed] = useState('1.0')
  const [capabilities, setCapabilities] = useState<AvatarCapabilities | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let disposed = false
    avatarApi
      .capabilities()
      .then((res) => {
        if (!disposed) setCapabilities(res)
      })
      .catch((err) => {
        if (!disposed) setError(apiError(err))
      })
    return () => {
      disposed = true
    }
  }, [])

  const available = capabilities?.available ?? false

  const handleSynthesize = () => {
    if (!script.trim()) {
      message.warning('请输入文本脚本')
      return
    }
    // 后端未接入真实数字人 API：绝不能用假进度条伪装成功，直接把原因显示出来
    message.error(capabilities?.message || '数字人服务当前不可用')
  }

  return (
    <Card title="数字人工作室">
      <Space direction="vertical" size="large" style={{ width: '100%' }}>
        {error && <Alert type="error" showIcon message="无法读取数字人能力" description={error} />}
        {capabilities && !available && (
          <Alert
            type="warning"
            showIcon
            message="功能未启用"
            description={
              capabilities.message ||
              '数字人合成尚未接入真实 API（HeyGen / D-ID）。剪辑、字幕与导出链路不受影响。'
            }
          />
        )}
        <Typography.Title level={5}>文本脚本</Typography.Title>
        <TextArea
          value={script}
          onChange={(e) => setScript(e.target.value)}
          rows={6}
          placeholder="输入数字人播报文本..."
          disabled={!available}
        />
        <Space wrap>
          <Select
            value={provider}
            onChange={setProvider}
            style={{ width: 120 }}
            disabled={!available}
            options={[
              { value: 'heygen', label: 'HeyGen' },
              { value: 'did', label: 'D-ID' },
            ]}
          />
          <Select
            value={speed}
            onChange={setSpeed}
            style={{ width: 120 }}
            disabled={!available}
            options={[
              { value: '0.5', label: '慢速 0.5x' },
              { value: '1.0', label: '正常 1.0x' },
              { value: '1.5', label: '快速 1.5x' },
            ]}
          />
        </Space>
        <Button type="primary" disabled={!available} onClick={handleSynthesize}>
          合成
        </Button>
      </Space>
    </Card>
  )
}

export default AvatarStudio
