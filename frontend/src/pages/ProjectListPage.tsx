import { useCallback, useEffect, useState } from 'react'
import { Typography, Button, List, Card, Space, Modal, Input, message } from 'antd'
import { PlusOutlined, DeleteOutlined, ReloadOutlined } from '@ant-design/icons'
import { useNavigate } from 'react-router-dom'
import { apiError } from '../api/backend'
import { projectsApi } from '../api/client'
import type { Project } from '../api/types'
import { useEDLStore } from '../stores/edlStore'
import { useProjectStore } from '../stores/projectStore'

const { Text } = Typography

function ProjectListPage() {
  const navigate = useNavigate()
  const { projects, setProjects, setCurrentProject, loading, setLoading, setError, error } =
    useProjectStore()
  const resetEdl = useEDLStore((s) => s.reset)
  const [createModalOpen, setCreateModalOpen] = useState(false)
  const [newProjectName, setNewProjectName] = useState('')
  const [creating, setCreating] = useState(false)

  const loadProjects = useCallback(async () => {
    setLoading(true)
    try {
      setProjects(await projectsApi.list(50))
      setError(null)
    } catch (err) {
      setError(apiError(err))
    } finally {
      setLoading(false)
    }
  }, [setProjects, setLoading, setError])

  useEffect(() => {
    void loadProjects()
  }, [loadProjects])

  const handleCreate = async () => {
    if (!newProjectName.trim()) {
      message.warning('请输入项目名称')
      return
    }
    setCreating(true)
    try {
      const project = await projectsApi.create(newProjectName.trim())
      message.success('项目创建成功')
      setCreateModalOpen(false)
      setNewProjectName('')
      await loadProjects()
      resetEdl()
      setCurrentProject(project)
      navigate('/workspace')
    } catch (err) {
      message.error(apiError(err))
    } finally {
      setCreating(false)
    }
  }

  const handleOpen = (project: Project) => {
    resetEdl()
    setCurrentProject(project)
    navigate('/workspace')
  }

  const handleDelete = (project: Project) => {
    Modal.confirm({
      title: '确认删除',
      content: `删除「${project.name}」会同时移除其剪辑版本与识别结果，且无法恢复。`,
      okText: '删除',
      okButtonProps: { danger: true },
      onOk: async () => {
        try {
          await projectsApi.delete(project.project_id)
          message.success('已删除')
          await loadProjects()
        } catch (err) {
          message.error(apiError(err))
        }
      },
    })
  }

  return (
    <Space direction="vertical" size="large" style={{ width: '100%' }}>
      <Space>
        <Button type="primary" icon={<PlusOutlined />} onClick={() => setCreateModalOpen(true)}>
          新建项目
        </Button>
        <Button icon={<ReloadOutlined />} loading={loading} onClick={() => void loadProjects()}>
          刷新列表
        </Button>
      </Space>

      {error && (
        <Card size="small">
          <Text type="danger">加载项目失败：{error}</Text>
        </Card>
      )}

      <Card title={`最近项目 (${projects.length})`}>
        <List
          loading={loading}
          dataSource={projects}
          locale={{ emptyText: '暂无项目，点击「新建项目」创建' }}
          renderItem={(project) => (
            <List.Item
              actions={[
                <Button key="open" type="link" onClick={() => handleOpen(project)}>
                  打开
                </Button>,
                <Button
                  key="delete"
                  type="link"
                  danger
                  icon={<DeleteOutlined />}
                  onClick={() => handleDelete(project)}
                >
                  删除
                </Button>,
              ]}
            >
              <List.Item.Meta
                title={project.name}
                description={
                  <Space direction="vertical" size={0}>
                    <Text type="secondary">ID: {project.project_id.slice(0, 8)}…</Text>
                    <Text type="secondary">{project.source_path || '未导入媒体'}</Text>
                    {project.duration_us > 0 && (
                      <Text type="secondary">
                        时长 {(project.duration_us / 1_000_000).toFixed(1)}s
                        {project.current_revision_id ? ' · 已有剪辑版本' : ''}
                      </Text>
                    )}
                  </Space>
                }
              />
            </List.Item>
          )}
        />
      </Card>

      <Modal
        title="新建项目"
        open={createModalOpen}
        onOk={() => void handleCreate()}
        onCancel={() => {
          setCreateModalOpen(false)
          setNewProjectName('')
        }}
        confirmLoading={creating}
        okText="创建"
        cancelText="取消"
      >
        <Input
          placeholder="请输入项目名称"
          value={newProjectName}
          onChange={(e) => setNewProjectName(e.target.value)}
          onPressEnter={() => void handleCreate()}
          autoFocus
        />
        <Text type="secondary" style={{ fontSize: 12 }}>
          创建后再导入媒体文件即可开始分析。
        </Text>
      </Modal>
    </Space>
  )
}

export default ProjectListPage
