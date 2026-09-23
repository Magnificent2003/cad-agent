import { useCallback, useEffect, useRef, useState } from 'react'
import {
  Check,
  CircleAlert,
  FileUp,
  FolderOpen,
  LoaderCircle,
  PanelLeft,
  RefreshCw,
  X,
} from 'lucide-react'
import ChatPanel from './components/ChatPanel'
import DrawingViewer from './components/DrawingViewer'
import ProjectSidebar from './components/ProjectSidebar'
import { api, readableError, saveBlob } from './api/client'
import { metadataInfo } from './api/metadata'
import { useSessionEvents } from './api/useSessionEvents'
import type { Drawing, Message, Project, Revision, SessionEvent, Workbench } from './api/types'

const delay = (milliseconds: number) =>
  new Promise((resolve) => window.setTimeout(resolve, milliseconds))
const latestFor = (workspace: Workbench, drawingId?: string | null) =>
  workspace.versions.find((v) => v.drawing_id === drawingId && v.revision_id === v.current_revision)

export default function App() {
  const [projects, setProjects] = useState<Project[]>([])
  const [projectId, setProjectId] = useState<string | null>(null)
  const [workspace, setWorkspace] = useState<Workbench | null>(null)
  const [collapsed, setCollapsed] = useState(() => {
    try {
      return window.innerWidth < 1100 || localStorage.getItem('cad-project-sidebar') === 'collapsed'
    } catch {
      return true
    }
  })
  // Viewing a historical file must not silently change the agent's input.
  const [targetDrawingId, setTargetDrawingId] = useState<string | null>(null)
  const [viewKey, setViewKey] = useState('')
  const [drawingCache, setDrawingCache] = useState<Record<string, Drawing>>({})
  const [revisionCache, setRevisionCache] = useState<Record<string, Revision[]>>({})
  const [layout, setLayout] = useState('Model')
  const [sessionId, setSessionId] = useState<string | null>(null)
  const [messages, setMessages] = useState<Message[]>([])
  const [messagesLoading, setMessagesLoading] = useState(false)
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const [stopping, setStopping] = useState(false)
  const [importing, setImporting] = useState(false)
  const [syncingResult, setSyncingResult] = useState(false)
  const [loading, setLoading] = useState(true)
  const [health, setHealth] = useState<'checking' | 'online' | 'offline'>('checking')
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [taskError, setTaskError] = useState('')
  const [downloading, setDownloading] = useState('')
  const [dragging, setDragging] = useState(false)
  const fileRef = useRef<HTMLInputElement>(null)
  const activeProject = useRef(projectId)
  const activeSession = useRef(sessionId)
  const targetRef = useRef(targetDrawingId)
  const workspaceSequence = useRef(0)
  const messageSequence = useRef(0)
  const messageTimer = useRef<number | undefined>(undefined)
  const sendLock = useRef(false)
  const stopLock = useRef(false)
  const handledTasks = useRef(new Set<string>())
  const dragDepth = useRef(0)
  activeProject.current = projectId
  activeSession.current = sessionId
  targetRef.current = targetDrawingId

  const refreshWorkspace = useCallback(async (id: string) => {
    const sequence = ++workspaceSequence.current
    const next = await api.workbench(id)
    if (activeProject.current === id && sequence === workspaceSequence.current) setWorkspace(next)
    return next
  }, [])
  const refreshMessages = useCallback(async (id: string) => {
    const sequence = ++messageSequence.current
    const next = await api.messages(id)
    if (activeSession.current === id && sequence === messageSequence.current) setMessages(next)
  }, [])
  const reloadProjects = useCallback(async () => {
    const [healthResult, projectResult] = await Promise.allSettled([api.health(), api.projects()])
    setHealth(healthResult.status === 'fulfilled' ? 'online' : 'offline')
    if (projectResult.status === 'fulfilled') {
      setProjects(projectResult.value)
      setProjectId((previous) =>
        previous && projectResult.value.some((p) => p.id === previous)
          ? previous
          : projectResult.value[0]?.id || null,
      )
      if (!projectResult.value.length) setLoading(false)
    } else {
      setError(readableError(projectResult.reason))
      setLoading(false)
    }
  }, [])
  useEffect(() => {
    void reloadProjects()
  }, [reloadProjects])
  useEffect(() => {
    try {
      localStorage.setItem('cad-project-sidebar', collapsed ? 'collapsed' : 'expanded')
    } catch {
      /* Optional preference. */
    }
    const close = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setCollapsed(true)
    }
    window.addEventListener('keydown', close)
    return () => window.removeEventListener('keydown', close)
  }, [collapsed])
  useEffect(() => {
    if (!notice) return
    const timer = window.setTimeout(() => setNotice(''), 6500)
    return () => window.clearTimeout(timer)
  }, [notice])

  useEffect(() => {
    setWorkspace(null)
    setTargetDrawingId(null)
    setViewKey('')
    setSessionId(null)
    setMessages([])
    setInput('')
    setTaskError('')
    setBusy(false)
    setStopping(false)
    setSyncingResult(false)
    if (!projectId) return
    setLoading(true)
    let cancelled = false
    let inFlight = false
    let initialized = false
    const refresh = async () => {
      if (inFlight) return
      inFlight = true
      try {
        const next = await refreshWorkspace(projectId)
        if (cancelled) return
        if (!initialized) {
          initialized = true
          const session = next.sessions.find((s) => s.engine === 'freecad')
          const version = latestFor(next, session?.drawing_id) || next.versions.at(-1)
          setTargetDrawingId(session?.drawing_id || version?.drawing_id || null)
          setViewKey(version?.key || '')
          setSessionId(session?.id || null)
          setBusy(session?.status === 'busy')
        }
        setLoading(false)
      } catch (cause) {
        if (!cancelled) {
          setError(readableError(cause))
          setLoading(false)
        }
      } finally {
        inFlight = false
      }
    }
    void refresh()
    const timer = window.setInterval(() => void refresh(), 2000)
    return () => {
      cancelled = true
      window.clearInterval(timer)
    }
  }, [projectId, refreshWorkspace])

  const viewVersion = workspace?.versions.find((v) => v.key === viewKey)
  const currentVersion = workspace ? latestFor(workspace, targetDrawingId) : undefined
  const drawing = targetDrawingId ? drawingCache[targetDrawingId] || null : null
  const viewDrawing = viewVersion ? drawingCache[viewVersion.drawing_id] || null : null
  const historical = Boolean(viewKey && currentVersion && viewKey !== currentVersion.key)
  const selectedMetadata =
    revisionCache[viewVersion?.drawing_id || '']?.find((r) => r.id === viewVersion?.revision_id)
      ?.metadata ||
    viewDrawing?.metadata ||
    {}
  const info = metadataInfo(selectedMetadata)
  const locked = busy || importing || syncingResult || loading

  useEffect(() => {
    setLayout('Model')
  }, [viewKey])
  useEffect(() => {
    if (!info.layouts.includes(layout)) setLayout(info.layouts[0] || 'Model')
  }, [layout, info.layouts.join('|')])
  useEffect(() => {
    let cancelled = false
    const ids = [
      ...new Set(
        [targetDrawingId, viewVersion?.drawing_id].filter((id): id is string => Boolean(id)),
      ),
    ]
    void Promise.all(
      ids.map(async (id) => {
        const [file, revisions] = await Promise.all([api.drawing(id), api.revisions(id)])
        if (!cancelled) {
          setDrawingCache((old) => ({ ...old, [id]: file }))
          setRevisionCache((old) => ({ ...old, [id]: revisions }))
        }
      }),
    ).catch((cause) => {
      if (!cancelled) setError(readableError(cause))
    })
    return () => {
      cancelled = true
    }
  }, [
    targetDrawingId,
    viewVersion?.drawing_id,
    viewVersion?.revision_id,
    currentVersion?.revision_id,
  ])
  useEffect(() => {
    setMessages([])
    setTaskError('')
    if (!sessionId) {
      setMessagesLoading(false)
      return
    }
    setMessagesLoading(true)
    void refreshMessages(sessionId)
      .catch((cause) => {
        if (activeSession.current === sessionId) setError(readableError(cause))
      })
      .finally(() => {
        if (activeSession.current === sessionId) setMessagesLoading(false)
      })
  }, [sessionId, refreshMessages])

  useEffect(() => {
    if (!sessionId || !projectId) return
    let cancelled = false
    let inFlight = false
    const refresh = async () => {
      if (inFlight) return
      inFlight = true
      try {
        const [files, status] = await Promise.all([
          api.artifacts(sessionId),
          api.sessionStatus(sessionId),
        ])
        if (cancelled || activeSession.current !== sessionId || activeProject.current !== projectId)
          return
        setTaskError(files.task?.error || '')
        if (!sendLock.current && !stopLock.current) {
          setBusy(status.status === 'busy')
          if (status.status !== 'busy') setStopping(false)
        }
        const task = files.task
        if (
          !task ||
          task.status !== 'completed' ||
          handledTasks.current.has(task.id) ||
          status.status === 'busy' ||
          sendLock.current
        )
          return
        const cadFiles = files.artifacts.filter((f) => f.task_id === task.id && f.drawing_id)
        // Wait for all CAD previews in this task, then adopt exactly one latest result.
        if (cadFiles.some((f) => ['queued', 'running'].includes(f.preview_status))) {
          setSyncingResult(true)
          return
        }
        const candidate = cadFiles.filter((f) => f.preview_status === 'succeeded').at(-1)
        if (!candidate || candidate.drawing_id === targetRef.current) {
          handledTasks.current.add(task.id)
          setSyncingResult(false)
          return
        }
        setSyncingResult(true)
        try {
          const adopted = await api.openArtifact(candidate.id)
          if (
            cancelled ||
            activeSession.current !== sessionId ||
            activeProject.current !== projectId
          )
            return
          const next = await refreshWorkspace(projectId)
          if (
            cancelled ||
            activeSession.current !== sessionId ||
            activeProject.current !== projectId
          )
            return
          setTargetDrawingId(adopted.drawing_id)
          setViewKey(latestFor(next, adopted.drawing_id)?.key || '')
          setNotice(
            `已显示新图纸：${latestFor(next, adopted.drawing_id)?.label || candidate.name}，可继续对话。`,
          )
          handledTasks.current.add(task.id)
          await refreshMessages(sessionId)
        } catch (cause) {
          if (!cancelled) setError(readableError(cause))
        } finally {
          if (!cancelled) setSyncingResult(false)
        }
      } catch {
        /* Other requests and connection state report network failures. */
      } finally {
        inFlight = false
      }
    }
    void refresh()
    const timer = window.setInterval(() => void refresh(), 2000)
    return () => {
      cancelled = true
      window.clearInterval(timer)
    }
  }, [sessionId, projectId, refreshWorkspace, refreshMessages])

  const onEvent = useCallback(
    (event: SessionEvent) => {
      if (
        event.type.startsWith('message.') ||
        event.type === 'session.idle' ||
        event.type === 'session.status'
      ) {
        if (messageTimer.current === undefined)
          messageTimer.current = window.setTimeout(() => {
            messageTimer.current = undefined
            if (activeSession.current)
              void refreshMessages(activeSession.current).catch(() => undefined)
          }, 250)
      }
      if (
        ['drawing.updated', 'artifacts.updated', 'job.updated'].includes(event.type) &&
        activeProject.current
      )
        void refreshWorkspace(activeProject.current).catch(() => undefined)
      // Busy/idle ownership comes from the business task, not a model's intermediate stop.
      if (event.type === 'session.error') setError('助手处理未完成，请检查任务提示后重试。')
    },
    [refreshMessages, refreshWorkspace],
  )
  const onReconnect = useCallback(() => {
    if (activeSession.current) void refreshMessages(activeSession.current).catch(() => undefined)
    if (activeProject.current) void refreshWorkspace(activeProject.current).catch(() => undefined)
  }, [refreshMessages, refreshWorkspace])
  const { connected, waitUntilConnected } = useSessionEvents(sessionId, onEvent, onReconnect)
  useEffect(
    () => () => {
      if (messageTimer.current !== undefined) window.clearTimeout(messageTimer.current)
    },
    [],
  )

  const selectProject = (id: string) => {
    if (locked) return
    activeProject.current = id
    activeSession.current = null
    setProjectId(id)
    setError('')
    if (window.innerWidth < 1100) setCollapsed(true)
  }
  const selectSession = (id: string) => {
    const session = workspace?.sessions.find((s) => s.id === id)
    if (!session || locked || !workspace) return
    activeSession.current = id
    setSessionId(id)
    setTargetDrawingId(session.drawing_id || null)
    setViewKey(latestFor(workspace, session.drawing_id)?.key || '')
    setBusy(session.status === 'busy')
    setStopping(false)
    setInput('')
  }
  const saveProject = async (name: string, id?: string) => {
    try {
      const project = id ? await api.renameProject(id, name) : await api.createProject(name)
      setProjects(await api.projects())
      if (id) {
        if (id === activeProject.current) await refreshWorkspace(id)
      } else selectProject(project.id)
    } catch (cause) {
      setError(readableError(cause))
      throw cause
    }
  }
  const upload = async (file: File) => {
    if (locked || !projectId) return
    if (!/\.(dwg|dxf)$/i.test(file.name)) {
      setError('请选择 DWG 或 DXF 格式的图纸。')
      return
    }
    setError('')
    setImporting(true)
    try {
      const result = await api.upload(file, projectId)
      for (let attempt = 0; attempt < 600; attempt++) {
        const job = await api.job(result.job_id)
        if (['completed', 'succeeded', 'success'].includes(job.status)) {
          const next = await refreshWorkspace(projectId)
          setTargetDrawingId(result.drawing_id)
          setViewKey(latestFor(next, result.drawing_id)?.key || '')
          setSessionId(null)
          setInput('')
          setMessages([])
          setNotice('图纸已加入当前项目，可以开始查看和对话。')
          return
        }
        if (['failed', 'cancelled', 'interrupted'].includes(job.status))
          throw new Error(job.error || '图纸导入未完成，请重试。')
        await delay(1500)
      }
      setNotice('图纸仍在后台处理中，完成后可从项目图纸列表选择。')
    } catch (cause) {
      setError(readableError(cause))
    } finally {
      setImporting(false)
    }
  }
  const createSession = async () => {
    if (!targetDrawingId || busy || importing || syncingResult) return null
    const result = await api.createSession(targetDrawingId)
    activeSession.current = result.id
    setSessionId(result.id)
    setMessages([])
    setViewKey(currentVersion?.key || '')
    if (projectId) await refreshWorkspace(projectId)
    return result.id
  }
  const send = async () => {
    if (sendLock.current || locked || historical || !input.trim() || !drawing?.current_revision)
      return
    sendLock.current = true
    const text = input.trim()
    const optimisticId = `pending-${Date.now()}`
    setError('')
    try {
      const target = sessionId || (await createSession())
      if (!target) return
      setBusy(true)
      await waitUntilConnected(target)
      setInput('')
      setMessages((old) => [
        ...old,
        { id: optimisticId, role: 'user', parts: [{ type: 'text', text }] },
      ])
      await api.send(target, text, drawing.current_revision)
      await refreshMessages(target)
    } catch (cause) {
      setError(readableError(cause))
      setBusy(false)
      setInput(text)
      setMessages((old) => old.filter((m) => m.id !== optimisticId))
    } finally {
      sendLock.current = false
    }
  }
  const stop = async () => {
    if (!sessionId || stopping) return
    stopLock.current = true
    setStopping(true)
    try {
      const result = await api.abort(sessionId)
      await refreshMessages(sessionId)
      setStopping(!result.model_stopped)
      setBusy(!result.model_stopped)
      setNotice(
        result.model_stopped ? '任务已停止。' : '已请求停止，正在等待 FreeCAD 中已开始的操作结束。',
      )
    } catch (cause) {
      setError(readableError(cause))
      setStopping(false)
    } finally {
      stopLock.current = false
    }
  }
  const download = async (format: 'dxf' | 'png' | 'dwg') => {
    if (!viewVersion || downloading) return
    setDownloading(format)
    setError('')
    const url = api.download(viewVersion.drawing_id, viewVersion.revision_id, layout, format)
    try {
      for (let attempt = 0; attempt < 120; attempt++) {
        const response = await fetch(url)
        if (response.status === 202) {
          const pending = await response.json().catch(() => ({}))
          if (pending.job_id) {
            const job = await api.job(pending.job_id)
            if (['failed', 'cancelled', 'interrupted'].includes(job.status))
              throw new Error(job.error || '导出未完成，请重试。')
          }
          await delay(1500)
          continue
        }
        if (!response.ok) {
          const body = await response.json().catch(() => ({}))
          throw new Error(
            typeof body.detail === 'string' ? body.detail : '导出暂不可用，请稍后重试。',
          )
        }
        saveBlob(await response.blob(), `${viewVersion.label}.${format}`)
        setNotice(`${format.toUpperCase()} 已准备好下载。`)
        return
      }
      throw new Error('导出仍在处理中，请稍后重试。')
    } catch (cause) {
      setError(readableError(cause))
    } finally {
      setDownloading('')
    }
  }

  return (
    <main
      className={`app-shell ${collapsed ? 'sidebar-collapsed' : ''}`}
      onDragEnter={(event) => {
        if (Array.from(event.dataTransfer.types).includes('Files')) {
          event.preventDefault()
          dragDepth.current++
          setDragging(true)
        }
      }}
      onDragOver={(event) => {
        if (Array.from(event.dataTransfer.types).includes('Files')) event.preventDefault()
      }}
      onDragLeave={(event) => {
        event.preventDefault()
        dragDepth.current--
        if (dragDepth.current <= 0) setDragging(false)
      }}
      onDrop={(event) => {
        event.preventDefault()
        dragDepth.current = 0
        setDragging(false)
        if (event.dataTransfer.files[0]) void upload(event.dataTransfer.files[0])
      }}
    >
      {!collapsed && (
        <button
          className="sidebar-backdrop"
          aria-label="关闭项目栏"
          onClick={() => setCollapsed(true)}
        />
      )}
      <ProjectSidebar
        projects={projects}
        selected={projectId}
        collapsed={collapsed}
        disabled={locked}
        onCollapse={() => setCollapsed(true)}
        onSelect={selectProject}
        onCreate={(name) => saveProject(name)}
        onRename={(id, name) => saveProject(name, id)}
      />
      <div className="workspace-shell">
        <header className="app-header">
          <button
            className="icon-button sidebar-toggle"
            onClick={() => setCollapsed((old) => !old)}
            aria-label={collapsed ? '展开项目栏' : '收起项目栏'}
            aria-expanded={!collapsed}
            title={collapsed ? '展开项目栏' : '收起项目栏'}
          >
            <PanelLeft size={19} />
          </button>
          <div className="project-breadcrumb">
            <FolderOpen size={17} />
            <div>
              <small>工程项目</small>
              <strong>
                {workspace?.project.name ||
                  projects.find((p) => p.id === projectId)?.name ||
                  '图纸工作台'}
              </strong>
            </div>
          </div>
          <div className="header-actions">
            <span className="project-version-count">{workspace?.versions.length || 0} 份图纸</span>
            <input
              ref={fileRef}
              type="file"
              className="visually-hidden"
              aria-label="上传 CAD 图纸"
              accept=".dwg,.dxf"
              onChange={(event) => {
                if (event.target.files?.[0]) void upload(event.target.files[0])
                event.target.value = ''
              }}
            />
            <button
              className="button upload-button"
              disabled={locked || !projectId}
              onClick={() => fileRef.current?.click()}
            >
              {importing ? <LoaderCircle size={15} className="spin" /> : <FileUp size={15} />}
              <span>{importing ? '正在导入' : '上传图纸'}</span>
            </button>
          </div>
        </header>
        {error && (
          <div className="alert-banner" role="alert">
            <CircleAlert size={16} />
            <span>{error}</span>
            <button
              className="icon-button"
              aria-label={health === 'offline' ? '重新连接' : '关闭提示'}
              onClick={() => {
                setError('')
                if (health === 'offline') void reloadProjects()
              }}
            >
              {health === 'offline' ? <RefreshCw size={15} /> : <X size={15} />}
            </button>
          </div>
        )}
        <div className="workbench">
          <ChatPanel
            drawing={drawing}
            currentLabel={currentVersion?.label || ''}
            taskError={taskError}
            transitioning={syncingResult || importing || loading}
            archived={Boolean(
              sessionId && workspace?.sessions.find((s) => s.id === sessionId)?.engine === 'legacy',
            )}
            sessions={workspace?.sessions || []}
            sessionId={sessionId}
            onSession={selectSession}
            onNewSession={() => {
              void createSession().catch((cause) => setError(readableError(cause)))
            }}
            messages={messages}
            messagesLoading={messagesLoading}
            input={input}
            onInput={setInput}
            onSend={() => void send()}
            onStop={() => void stop()}
            busy={busy}
            stopping={stopping}
            connected={connected}
            historical={historical}
            onCurrentRevision={() => setViewKey(currentVersion?.key || '')}
          />
          <DrawingViewer
            drawing={viewDrawing}
            versions={workspace?.versions || []}
            selectedKey={viewKey}
            currentKey={currentVersion?.key || ''}
            onVersion={setViewKey}
            revision={viewVersion?.revision_id || ''}
            layout={layout}
            onLayout={setLayout}
            historical={historical}
            onCurrent={() => setViewKey(currentVersion?.key || '')}
            processing={importing || busy || syncingResult || loading}
            onUpload={() => fileRef.current?.click()}
            metadata={selectedMetadata}
            artifacts={workspace?.artifacts || []}
            downloading={downloading}
            onDownload={(format) => void download(format)}
          />
        </div>
        <footer className="status-bar">
          <div>
            <span className={`service-dot ${health}`} />
            <span>
              {health === 'online'
                ? '服务已连接'
                : health === 'checking'
                  ? '正在连接服务'
                  : '服务未连接'}
            </span>
            <span className="status-divider" />
            <span>
              {importing
                ? '正在导入图纸'
                : syncingResult
                  ? '正在更新成果预览'
                  : stopping
                    ? '正在停止任务'
                    : busy
                      ? '助手处理中'
                      : '就绪'}
            </span>
          </div>
          <div>
            {viewVersion && (
              <>
                <span>单位：{info.unit}</span>
                <span className="status-divider" />
                <span>{info.layerCount} 图层</span>
              </>
            )}
            <span className="status-divider" />
            <span>原图与历史图纸保留</span>
          </div>
        </footer>
      </div>
      {notice && (
        <div className="notice-toast" role="status">
          <Check size={16} />
          <span>{notice}</span>
          <button className="icon-button" onClick={() => setNotice('')} aria-label="关闭提示">
            <X size={14} />
          </button>
        </div>
      )}
      {dragging && (
        <div className="drop-overlay">
          <FileUp size={38} />
          <h2>{locked ? '请等待当前任务完成' : '松开鼠标，加入当前项目'}</h2>
          <p>支持 DWG 和 DXF 文件</p>
        </div>
      )}
    </main>
  )
}
