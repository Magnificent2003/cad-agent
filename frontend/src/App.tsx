import { useCallback, useEffect, useRef, useState } from 'react'
import {
  Check,
  ChevronDown,
  CircleAlert,
  Download,
  FileStack,
  FileUp,
  LoaderCircle,
  PanelLeft,
  RefreshCw,
  Shield,
  X,
} from 'lucide-react'
import ChatPanel from './components/ChatPanel'
import DrawingViewer from './components/DrawingViewer'
import { api, readableError, revisionLabel } from './api/client'
import { metadataInfo } from './api/metadata'
import { useSessionEvents } from './api/useSessionEvents'
import type { Drawing, Message, Revision, Session, SessionEvent } from './api/types'

const delay = (milliseconds: number) =>
  new Promise((resolve) => window.setTimeout(resolve, milliseconds))

export default function App() {
  const [drawings, setDrawings] = useState<Drawing[]>([])
  const [drawingId, setDrawingId] = useState<string | null>(null)
  const [drawing, setDrawing] = useState<Drawing | null>(null)
  const [revisions, setRevisions] = useState<Revision[]>([])
  const [revision, setRevision] = useState('')
  const [layout, setLayout] = useState('Model')
  const [sessions, setSessions] = useState<Session[]>([])
  const [sessionId, setSessionId] = useState<string | null>(null)
  const [messages, setMessages] = useState<Message[]>([])
  const [messagesLoading, setMessagesLoading] = useState(false)
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const [stopping, setStopping] = useState(false)
  const [importing, setImporting] = useState(false)
  const [loading, setLoading] = useState(true)
  const [health, setHealth] = useState<'checking' | 'online' | 'offline'>('checking')
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [downloadMenu, setDownloadMenu] = useState(false)
  const [downloading, setDownloading] = useState('')
  const [dragging, setDragging] = useState(false)
  const fileRef = useRef<HTMLInputElement>(null)
  const activeDrawing = useRef<string | null>(null)
  const activeSession = useRef<string | null>(null)
  const drawingRef = useRef<Drawing | null>(null)
  const revisionRef = useRef('')
  const refreshTimer = useRef<number | undefined>(undefined)
  const messageSequence = useRef(0)
  const sendLock = useRef(false)
  const stopLock = useRef(false)
  const dragDepth = useRef(0)
  activeDrawing.current = drawingId
  activeSession.current = sessionId
  drawingRef.current = drawing
  revisionRef.current = revision

  const refreshDrawing = useCallback(async (id: string, forceCurrent = false) => {
    const [nextDrawing, nextRevisions] = await Promise.all([api.drawing(id), api.revisions(id)])
    if (activeDrawing.current !== id) return
    const wasCurrent =
      !revisionRef.current || revisionRef.current === drawingRef.current?.current_revision
    setDrawing(nextDrawing)
    setRevisions(nextRevisions)
    setRevision((previous) =>
      forceCurrent || wasCurrent || !nextRevisions.some((item) => item.id === previous)
        ? nextDrawing.current_revision || ''
        : previous,
    )
    setDrawings((previous) =>
      previous.some((item) => item.id === id)
        ? previous.map((item) => (item.id === id ? nextDrawing : item))
        : [...previous, nextDrawing],
    )
  }, [])

  const refreshMessages = useCallback(async (id: string) => {
    const sequence = ++messageSequence.current
    const nextMessages = await api.messages(id)
    if (activeSession.current === id && sequence === messageSequence.current)
      setMessages(nextMessages)
  }, [])

  const reload = useCallback(async () => {
    setLoading(true)
    const [healthResult, drawingsResult] = await Promise.allSettled([api.health(), api.drawings()])
    setHealth(healthResult.status === 'fulfilled' ? 'online' : 'offline')
    if (drawingsResult.status === 'fulfilled') {
      setDrawings(drawingsResult.value)
      setDrawingId((previous) =>
        previous && drawingsResult.value.some((item) => item.id === previous)
          ? previous
          : drawingsResult.value[0]?.id || null,
      )
    } else setError(readableError(drawingsResult.reason))
    setLoading(false)
  }, [])

  useEffect(() => {
    void reload()
  }, [reload])
  useEffect(() => {
    if (!notice) return
    const timer = window.setTimeout(() => setNotice(''), 6000)
    return () => window.clearTimeout(timer)
  }, [notice])

  useEffect(() => {
    setDrawing(null)
    setRevisions([])
    setRevision('')
    setLayout('Model')
    setSessions([])
    setSessionId(null)
    setMessages([])
    setInput('')
    setBusy(false)
    setStopping(false)
    if (!drawingId) return
    let cancelled = false
    void refreshDrawing(drawingId, true).catch((cause) => {
      if (!cancelled) setError(readableError(cause))
    })
    void api
      .sessions(drawingId)
      .then((items) => {
        if (cancelled) return
        setSessions(items)
        setSessionId(items[0]?.id || null)
        setBusy(items[0]?.status === 'busy')
      })
      .catch((cause) => {
        if (!cancelled) setError(readableError(cause))
      })
    return () => {
      cancelled = true
    }
  }, [drawingId, refreshDrawing])

  useEffect(() => {
    setMessages([])
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

  const onEvent = useCallback(
    (event: SessionEvent) => {
      const properties = (event.properties || {}) as Record<string, unknown>
      if (event.type.startsWith('message.')) {
        if (refreshTimer.current === undefined)
          refreshTimer.current = window.setTimeout(() => {
            refreshTimer.current = undefined
            if (activeSession.current)
              void refreshMessages(activeSession.current).catch((cause) =>
                setError(readableError(cause)),
              )
          }, 250)
      }
      if (event.type === 'session.status' || event.type === 'session.idle') {
        const rawStatus = event.status || properties.status
        const status =
          typeof rawStatus === 'string' ? rawStatus : (rawStatus as { type?: string })?.type
        if (status === 'busy' || status === 'retry') setBusy(true)
        if ((status === 'idle' || event.type === 'session.idle') && !stopLock.current) {
          setBusy(false)
          setStopping(false)
          if (activeSession.current)
            void refreshMessages(activeSession.current).catch((cause) =>
              setError(readableError(cause)),
            )
        }
      }
      if (event.type === 'session.error' || event.type === 'error') {
        const rawError = event.error || properties.error
        const message =
          typeof rawError === 'string'
            ? rawError
            : (rawError as { message?: string; data?: { message?: string } })?.message ||
              (rawError as { data?: { message?: string } })?.data?.message
        setError(readableError(new Error(message || '助手处理未完成，请重试。')))
        if (!stopLock.current) {
          setBusy(false)
          setStopping(false)
        }
      }
      if (event.type === 'drawing.updated' && activeDrawing.current) {
        const eventDrawing = event.drawing_id || properties.drawing_id
        if (!eventDrawing || eventDrawing === activeDrawing.current) {
          void refreshDrawing(activeDrawing.current).catch((cause) =>
            setError(readableError(cause)),
          )
          setNotice('图纸新版本已保存，预览已同步。')
        }
      }
      if (event.type === 'job.updated') {
        const status = event.status || properties.status
        if (!stopLock.current && (status === 'cancelled' || status === 'interrupted')) {
          setBusy(false)
          setStopping(false)
          setNotice('任务已停止。')
        }
      }
    },
    [refreshDrawing, refreshMessages],
  )

  const onReconnect = useCallback(() => {
    if (activeSession.current)
      void refreshMessages(activeSession.current).catch((cause) => setError(readableError(cause)))
    const targetSession = activeSession.current
    if (targetSession)
      void api
        .sessionStatus(targetSession)
        .then((result) => {
          if (activeSession.current === targetSession && !sendLock.current && !stopLock.current)
            setBusy(result.status === 'busy')
        })
        .catch((cause) => setError(readableError(cause)))
    if (activeDrawing.current)
      void refreshDrawing(activeDrawing.current).catch((cause) => setError(readableError(cause)))
  }, [refreshMessages, refreshDrawing])
  const { connected, waitUntilConnected } = useSessionEvents(sessionId, onEvent, onReconnect)
  useEffect(
    () => () => {
      if (refreshTimer.current !== undefined) window.clearTimeout(refreshTimer.current)
    },
    [],
  )

  const upload = async (file: File) => {
    if (importing || busy) return
    if (!/\.(dwg|dxf)$/i.test(file.name)) {
      setError('请选择 DWG 或 DXF 格式的图纸。')
      return
    }
    setError('')
    setNotice('')
    setImporting(true)
    try {
      const result = await api.upload(file)
      setDrawingId(result.drawing_id)
      for (let attempt = 0; attempt < 600; attempt++) {
        const job = await api.job(result.job_id)
        if (['completed', 'succeeded', 'success'].includes(job.status)) {
          await refreshDrawing(result.drawing_id, true)
          setNotice('图纸导入完成，可以开始查看和对话。')
          return
        }
        if (['failed', 'cancelled', 'interrupted'].includes(job.status)) {
          await refreshDrawing(result.drawing_id, true)
          throw new Error(job.error || '图纸导入未完成，请重试。')
        }
        await delay(1500)
      }
      setNotice('图纸仍在后台处理中，请稍后刷新查看。')
    } catch (cause) {
      setError(readableError(cause))
    } finally {
      setImporting(false)
    }
  }

  const createSession = async () => {
    if (!drawingId || busy) return null
    const result = await api.createSession(drawingId)
    setSessions((previous) => [result, ...previous])
    setSessionId(result.id)
    setMessages([])
    return result.id
  }

  const send = async () => {
    if (
      sendLock.current ||
      busy ||
      !input.trim() ||
      !drawing?.current_revision ||
      revision !== drawing.current_revision
    )
      return
    sendLock.current = true
    const text = input.trim()
    const expectedRevision = drawing.current_revision
    const optimisticId = `pending-${Date.now()}`
    setError('')
    try {
      const target = sessionId || (await createSession())
      if (!target) return
      setBusy(true)
      await waitUntilConnected(target)
      setBusy(true)
      setInput('')
      setMessages((previous) => [
        ...previous,
        { id: optimisticId, role: 'user', parts: [{ type: 'text', text }] },
      ])
      await api.send(target, text, expectedRevision)
      await refreshMessages(target)
    } catch (cause) {
      setError(readableError(cause))
      setBusy(false)
      setInput(text)
      setMessages((previous) => previous.filter((item) => item.id !== optimisticId))
      if (activeDrawing.current) void refreshDrawing(activeDrawing.current).catch(() => undefined)
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
      for (const originalJob of result.jobs || []) {
        let job = originalJob
        while (['queued', 'running', 'cancelling'].includes(job.status)) {
          await delay(1000)
          job = await api.job(job.id)
        }
      }
      await refreshMessages(sessionId)
      setStopping(false)
      if (result.model_stopped) {
        setBusy(false)
        setNotice('任务已停止。')
      } else {
        setBusy(true)
        setError('图纸写入已停止，模型停止尚未确认。请稍后再次点击停止。')
      }
    } catch (cause) {
      setError(readableError(cause))
      setStopping(false)
    } finally {
      stopLock.current = false
    }
  }

  const download = async (format: 'dxf' | 'png' | 'dwg') => {
    if (!drawing || !revision || downloading) return
    setDownloadMenu(false)
    setDownloading(format)
    setError('')
    const url = api.download(drawing.id, revision, layout, format)
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
        const objectUrl = URL.createObjectURL(await response.blob())
        const link = document.createElement('a')
        link.href = objectUrl
        link.download = `${drawing.name.replace(/\.(dwg|dxf)$/i, '')}_${revisionLabel(revision)}.${format}`
        document.body.appendChild(link)
        link.click()
        link.remove()
        window.setTimeout(() => URL.revokeObjectURL(objectUrl), 1000)
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

  const selectedMetadata =
    revisions.find((item) => item.id === revision)?.metadata || drawing?.metadata || {}
  const info = metadataInfo(selectedMetadata)
  useEffect(() => {
    if (!info.layouts.includes(layout)) setLayout(info.layouts[0] || 'Model')
  }, [layout, info.layouts.join('|')])
  const historical = Boolean(drawing?.current_revision && revision !== drawing.current_revision)

  return (
    <main
      className="app-shell"
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
      <header className="app-header">
        <a className="brand" href="/" aria-label="CAD Agent 工作台">
          <span className="brand-mark">
            <svg width="22" height="24" viewBox="0 0 22 24" fill="none" aria-hidden="true">
              <path
                d="M11 2L21 7.5V16.5L11 22L1 16.5V7.5L11 2Z"
                stroke="currentColor"
                strokeWidth="1.5"
              />
              <path
                d="M11 2V11.5M1 7.5L11 13L21 7.5M11 13V22M6 10V15L11 18L16 15V10"
                stroke="currentColor"
                strokeWidth="1.5"
              />
            </svg>
          </span>
          <span>
            CAD<span className="brand-light"> Agent</span>
          </span>
        </a>
        <span className="header-divider" />
        <div className="workspace-name">
          <PanelLeft size={15} />
          <span>图纸工作台</span>
        </div>
        <div className="drawing-picker">
          <FileStack size={15} />
          <select
            aria-label="选择图纸"
            value={drawingId || ''}
            disabled={!drawings.length || importing || busy}
            onChange={(event) => setDrawingId(event.target.value)}
          >
            {!drawings.length && (
              <option value="">{loading ? '正在读取图纸…' : '尚未选择图纸'}</option>
            )}
            {drawings.map((item) => (
              <option value={item.id} key={item.id}>
                {item.name}
              </option>
            ))}
          </select>
        </div>
        <div className="header-actions">
          {drawing?.current_revision && (
            <span className="current-version">
              <i />
              {revisionLabel(drawing.current_revision)}
            </span>
          )}
          <input
            type="file"
            ref={fileRef}
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
            disabled={importing || busy}
            onClick={() => fileRef.current?.click()}
          >
            {importing ? <LoaderCircle size={15} className="spin" /> : <FileUp size={15} />}
            <span>{importing ? '正在导入' : '上传图纸'}</span>
          </button>
          <div className="download-control">
            <button
              className="button button-primary"
              disabled={!revision || Boolean(downloading)}
              onClick={() => setDownloadMenu((value) => !value)}
              aria-expanded={downloadMenu}
            >
              {downloading ? <LoaderCircle size={15} className="spin" /> : <Download size={15} />}
              <span>{downloading ? '正在导出' : '下载'}</span>
              <ChevronDown size={13} />
            </button>
            {downloadMenu && (
              <>
                <button
                  className="menu-backdrop"
                  aria-label="关闭下载菜单"
                  onClick={() => setDownloadMenu(false)}
                />
                <div className="download-menu">
                  <span>下载所选版本 {revisionLabel(revision)}</span>
                  {(['dxf', 'png', 'dwg'] as const).map((format) => (
                    <button key={format} onClick={() => void download(format)}>
                      <span>{format.toUpperCase()}</span>
                      <small>
                        {format === 'dxf'
                          ? '可编辑图纸'
                          : format === 'png'
                            ? '当前布局预览'
                            : '转换后下载'}
                      </small>
                      <Download size={13} />
                    </button>
                  ))}
                </div>
              </>
            )}
          </div>
        </div>
      </header>
      {error && (
        <div className="alert-banner" role="alert">
          <CircleAlert size={16} />
          <span>{error}</span>
          <button
            onClick={() => {
              setError('')
              if (health === 'offline') void reload()
            }}
            className="icon-button"
            aria-label={health === 'offline' ? '重新连接' : '关闭提示'}
          >
            {health === 'offline' ? <RefreshCw size={15} /> : <X size={15} />}
          </button>
        </div>
      )}
      {notice && (
        <div className="notice-toast" role="status">
          <Check size={16} />
          <span>{notice}</span>
          <button className="icon-button" onClick={() => setNotice('')} aria-label="关闭提示">
            <X size={14} />
          </button>
        </div>
      )}
      <div className="workbench">
        <ChatPanel
          drawing={drawing}
          sessions={sessions}
          sessionId={sessionId}
          onSession={(id) => {
            setSessionId(id)
            setBusy(sessions.find((item) => item.id === id)?.status === 'busy')
            setStopping(false)
          }}
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
          onCurrentRevision={() => setRevision(drawing?.current_revision || '')}
        />
        <DrawingViewer
          drawing={drawing}
          revisions={revisions}
          revision={revision}
          onRevision={setRevision}
          layout={layout}
          onLayout={setLayout}
          processing={importing || busy}
          onUpload={() => fileRef.current?.click()}
          metadata={selectedMetadata}
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
            {importing ? '正在导入图纸' : stopping ? '正在停止任务' : busy ? '助手处理中' : '就绪'}
          </span>
        </div>
        <div>
          {drawing?.current_revision && (
            <>
              <span>单位：{info.unit}</span>
              <span className="status-divider" />
              <span>{info.layerCount} 图层</span>
              <span className="status-divider" />
            </>
          )}
          <Shield size={12} />
          <span>规范审查未运行</span>
        </div>
      </footer>
      {dragging && (
        <div className="drop-overlay">
          <FileUp size={38} />
          <h2>{importing || busy ? '请等待当前任务完成' : '松开鼠标，导入图纸'}</h2>
          <p>支持 DWG 和 DXF 文件</p>
        </div>
      )}
    </main>
  )
}
