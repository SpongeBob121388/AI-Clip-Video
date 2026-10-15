import { useEffect, useState } from 'react'
import { Alert, Button, Card, Form, Input, Select, Space, Tag, message } from 'antd'
import { apiError } from '../api/backend'
import { settingsApi } from '../api/client'
import type { LLMTestResult } from '../api/types'
import { useSettingsStore } from '../stores/settingsStore'

const MODEL_SIZES = ['tiny', 'base', 'small', 'medium', 'large']

function SettingsPage() {
  const store = useSettingsStore()
  const [llmApiKey, setLlmApiKey] = useState('')
  const [avatarApiKey, setAvatarApiKey] = useState('')
  const [testing, setTesting] = useState(false)
  const [saving, setSaving] = useState(false)
  const [testResult, setTestResult] = useState<LLMTestResult | null>(null)

  useEffect(() => {
    if (!store.loaded) void store.hydrate()
  }, [store])

  const handleTestConnection = async () => {
    if (!llmApiKey) {
      setTestResult({ success: false, message: '请先输入 API Key', latency_ms: 0 })
      return
    }
    setTesting(true)
    setTestResult(null)
    try {
      setTestResult(
        await settingsApi.testLlm({
          api_key: llmApiKey,
          base_url: store.llmBaseUrl || 'https://api.openai.com/v1',
          model: 'gpt-4o-mini',
        }),
      )
    } catch (err) {
      setTestResult({ success: false, message: apiError(err), latency_ms: 0 })
    } finally {
      setTesting(false)
    }
  }

  const handleSave = async () => {
    setSaving(true)
    try {
      const text = await store.save({
        llm_api_key: llmApiKey || undefined,
        avatar_api_key: avatarApiKey || undefined,
      })
      message.success(text)
      setLlmApiKey('')
      setAvatarApiKey('')
    } catch (err) {
      message.error(apiError(err))
    } finally {
      setSaving(false)
    }
  }

  const asrOptions = (store.capabilities?.asr_engines ?? ['faster_whisper']).map((engine) => ({
    value: engine,
    label: engine,
  }))

  return (
    <Card title="设置">
      <Form layout="vertical">
        {!store.credentialPersistent && (
          <Alert
            type="warning"
            showIcon
            style={{ marginBottom: 16 }}
            message="凭据后端不可用"
            description="API Key 只会保存在当前进程内存中，重启即失效。请检查 Windows 凭据管理器是否可用。"
          />
        )}
        {store.error && (
          <Alert
            type="error"
            showIcon
            style={{ marginBottom: 16 }}
            message="读取设置失败"
            description={store.error}
          />
        )}

        <Form.Item
          label="ASR 引擎"
          extra={
            asrOptions.length === 0
              ? '未安装 faster-whisper：分析会跳过语音识别，仅用音量检测静音段'
              : '只列出本机真正可用的引擎'
          }
        >
          <Select
            value={store.asrEngine}
            onChange={store.setAsrEngine}
            options={asrOptions}
            disabled={asrOptions.length === 0}
            placeholder={asrOptions.length === 0 ? '无可用引擎' : undefined}
          />
        </Form.Item>
        <Form.Item label="ASR 模型大小">
          <Select
            value={store.asrModelSize}
            onChange={store.setAsrModelSize}
            options={MODEL_SIZES.map((size) => ({ value: size, label: size }))}
          />
        </Form.Item>

        <Card type="inner" title="LLM 配置" style={{ marginBottom: 16 }}>
          <Form.Item label="LLM 端点 URL">
            <Input
              value={store.llmBaseUrl}
              onChange={(e) => store.setLlmBaseUrl(e.target.value)}
              placeholder="https://api.deepseek.com/v1"
            />
          </Form.Item>
          <Form.Item
            label={
              <Space>
                LLM API Key
                {store.hasLlmApiKey && <Tag color="green">已配置</Tag>}
              </Space>
            }
            extra="出于安全考虑，后端不会回读密钥；留空表示保持原值不变。"
          >
            <Input.Password
              value={llmApiKey}
              onChange={(e) => setLlmApiKey(e.target.value)}
              placeholder="sk-..."
            />
          </Form.Item>
          <Form.Item
            label={
              <Space>
                数字人 API Key
                {store.hasAvatarApiKey && <Tag color="green">已配置</Tag>}
              </Space>
            }
            extra={store.capabilities?.avatar_message || undefined}
          >
            <Input.Password
              value={avatarApiKey}
              onChange={(e) => setAvatarApiKey(e.target.value)}
              placeholder="选填"
            />
          </Form.Item>
          <Form.Item>
            <Space direction="vertical" style={{ width: '100%' }}>
              <Space wrap>
                <Button loading={testing} onClick={() => void handleTestConnection()}>
                  测试连接
                </Button>
                <Button type="primary" loading={saving} onClick={() => void handleSave()}>
                  保存设置
                </Button>
              </Space>

              {testResult && (
                <Alert
                  type={testResult.success ? 'success' : 'error'}
                  message={testResult.message}
                  description={testResult.latency_ms > 0 ? `延迟：${testResult.latency_ms}ms` : undefined}
                  showIcon
                  closable
                  onClose={() => setTestResult(null)}
                />
              )}
            </Space>
          </Form.Item>
        </Card>
      </Form>
    </Card>
  )
}

export default SettingsPage
