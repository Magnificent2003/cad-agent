import { useEffect, useRef } from 'react'
import {
  ArrowDown,
  ArrowRight,
  ArrowUp,
  Check,
  ChevronRight,
  CircleAlert,
  FileSearch,
  History,
  LoaderCircle,
  MessageSquare,
  Plus,
  ShieldCheck,
  Sparkles,
  Square,
  Wrench,
} from 'lucide-react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { metadataInfo } from '../api/metadata'
import { readableError, revisionLabel } from '../api/client'
import type { Drawing, Message, MessagePart, Session } from '../api/types'

interface Props {
  drawing: Drawing | null
  sessions: Session[]
  sessionId: string | null
  onSession: (id: string) => void
  onNewSession: () => void
  messages: Message[]
  messagesLoading: boolean
  input: string
  onInput: (input: string) => void
  onSend: () => void
  onStop: () => void
  busy: boolean
  stopping: boolean
  connected: boolean
  historical: boolean
  onCurrentRevision: () => void
}

const toolNames: Record<string, string> = {
  summary: '读取图纸概况',
  inspect: '读取图纸对象',
  search_text: '检索图纸文字',
  get_entities: '读取图元',
  query: '查询图纸',
  render: '查看图纸预览',
  preview: '查看图纸预览',
  edit: '生成修改版本',
  apply_operations: '执行图纸修改',
  check: '检查图纸',
  get_job: '确认处理结果',
  rag_search: '查询规范依据',
  export: '导出图纸',
}

function ToolPart({ part }: { part: MessagePart }) {
  const name =
    Object.entries(toolNames).find(([key]) => part.tool?.includes(key))?.[1] || '处理图纸任务'
  const status = part.state?.status
  return (
    <details className="tool-part">
      <summary>
        {status === 'completed' ? (
          <Check size={13} />
        ) : status === 'error' ? (
          <CircleAlert size={13} />
        ) : (
          <Wrench size={13} />
        )}
        <span>{name}</span>
        <span className="tool-status">
          {status === 'completed' ? '已完成' : status === 'error' ? '未完成' : '处理中'}
        </span>
        <ChevronRight size={13} />
      </summary>
      <p>
        {status === 'error'
          ? readableError(new Error(part.state?.error || '处理未完成，请重试。'))
          : status === 'completed'
            ? '已返回处理结果，助手将继续整理。'
            : '正在读取或处理当前图纸。'}
      </p>
    </details>
  )
}

export default function ChatPanel(props: Props) {
  const {
    drawing,
    sessions,
    sessionId,
    onSession,
    onNewSession,
    messages,
    messagesLoading,
    input,
    onInput,
    onSend,
    onStop,
    busy,
    stopping,
    connected,
    historical,
    onCurrentRevision,
  } = props
  const scrollRef = useRef<HTMLDivElement>(null)
  const followRef = useRef(true)
  const inputRef = useRef<HTMLTextAreaElement>(null)
  const info = metadataInfo(drawing?.metadata)
  const visibleMessages = messages.filter(
    (message) =>
      message.role === 'user' ||
      message.error ||
      message.parts.some((part) => (part.type === 'text' && part.text) || part.type === 'tool'),
  )
  const disabled = !drawing?.current_revision || historical || busy || messagesLoading

  useEffect(() => {
    if (followRef.current)
      scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: 'smooth' })
  }, [messages, busy])
  useEffect(() => {
    followRef.current = true
  }, [sessionId])
  useEffect(() => {
    if (inputRef.current) {
      inputRef.current.style.height = 'auto'
      inputRef.current.style.height = `${Math.min(160, Math.max(64, inputRef.current.scrollHeight))}px`
    }
  }, [input])

  const usePrompt = (prompt: string) => {
    onInput(prompt)
    inputRef.current?.focus()
  }

  return (
    <section className="chat-panel" aria-label="智能助手对话">
      <div className="panel-heading chat-heading">
        <div className="panel-title">
          <MessageSquare size={16} />
          <span>智能助手</span>
          {sessionId && (
            <span
              className={`connection-dot ${connected ? 'connected' : ''}`}
              title={connected ? '对话已连接' : '对话正在重连'}
            />
          )}
        </div>
        <div className="chat-heading-actions">
          {sessions.length > 0 && (
            <label className="session-select">
              <History size={15} />
              <select
                aria-label="历史对话"
                value={sessionId || ''}
                disabled={busy}
                onChange={(event) => onSession(event.target.value)}
              >
                {!sessionId && <option value="">选择对话</option>}
                {sessions.map((session) => (
                  <option key={session.id} value={session.id}>
                    {session.title || '图纸对话'}
                  </option>
                ))}
              </select>
            </label>
          )}
          <button
            className="icon-button"
            title="新建对话"
            aria-label="新建对话"
            disabled={!drawing?.current_revision || busy}
            onClick={onNewSession}
          >
            <Plus size={17} />
          </button>
        </div>
      </div>
      <div
        className="chat-scroll"
        ref={scrollRef}
        onScroll={() => {
          const el = scrollRef.current
          if (el) followRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 100
        }}
      >
        {!visibleMessages.length && !messagesLoading && (
          <div className="chat-welcome">
            <div className="assistant-avatar welcome-avatar">
              <Sparkles size={21} />
            </div>
            <p className="eyebrow">YOUR DRAWING COPILOT</p>
            <h1>
              让图纸信息，
              <br />
              成为可用的答案。
            </h1>
            <p className="welcome-copy">
              从理解图纸到精确修改，用自然语言推进工作，每一步都有迹可循。
            </p>
            <div className="capability-list">
              <div>
                <FileSearch size={16} />
                <span>读取文字、图层与图元信息</span>
              </div>
              <div>
                <History size={16} />
                <span>修改生成新版本，随时回看</span>
              </div>
              <div>
                <ShieldCheck size={16} />
                <span>原图保留，操作结果可追溯</span>
              </div>
            </div>
            {drawing?.current_revision && (
              <div className="drawing-summary">
                <span className="summary-heading">
                  <Check size={14} />
                  图纸已就绪
                </span>
                <strong>{drawing.name}</strong>
                <div>
                  <span>{info.layerCount} 个图层</span>
                  <i />
                  <span>{info.entityCount.toLocaleString()} 个图元</span>
                  <i />
                  <span>{info.unit}</span>
                </div>
              </div>
            )}
            <div className="suggestions">
              <span className="suggestions-title">
                {drawing ? '可以从这些问题开始' : '上传图纸后，试着问问'}
              </span>
              {[
                '概括这张图纸的内容、单位和图层信息',
                '提取图纸中的施工说明文字',
                '列出图纸中需要人工确认的问题',
              ].map((prompt) => (
                <button
                  disabled={!drawing?.current_revision}
                  key={prompt}
                  onClick={() => usePrompt(prompt)}
                >
                  <span>{prompt}</span>
                  <ArrowRight size={14} />
                </button>
              ))}
            </div>
          </div>
        )}
        {messagesLoading && (
          <div className="chat-loading">
            <LoaderCircle size={18} className="spin" />
            正在读取对话
          </div>
        )}
        {visibleMessages.map((message) => (
          <article className={`message message-${message.role}`} key={message.id}>
            <div className="message-label">
              {message.role === 'assistant' ? (
                <>
                  <span className="assistant-avatar">
                    <Sparkles size={13} />
                  </span>
                  <strong>CAD 助手</strong>
                </>
              ) : (
                <strong>你</strong>
              )}
            </div>
            <div className="message-body">
              {message.parts.map((part, index) =>
                part.type === 'text' && part.text ? (
                  <div className="markdown" key={part.id || index}>
                    <ReactMarkdown remarkPlugins={[remarkGfm]}>{part.text}</ReactMarkdown>
                  </div>
                ) : part.type === 'tool' ? (
                  <ToolPart part={part} key={part.id || index} />
                ) : null,
              )}
              {message.error && (
                <p className="message-error">
                  <CircleAlert size={13} />
                  {readableError(new Error(message.error))}
                </p>
              )}
            </div>
          </article>
        ))}
        {busy && (
          <div className="working-indicator" role="status">
            <LoaderCircle size={15} className="spin" />
            <span>{stopping ? '正在停止任务，确认处理状态…' : '助手正在处理…'}</span>
          </div>
        )}
      </div>
      <div className="composer-area">
        {historical && (
          <div className="history-composer">
            <History size={14} />
            <span>历史版本仅供查看</span>
            <button onClick={onCurrentRevision}>
              回到当前版本
              <ArrowRight size={13} />
            </button>
          </div>
        )}
        {sessionId && !connected && (
          <div className="connection-notice">
            <LoaderCircle size={12} className="spin" />
            正在恢复对话连接，历史消息将自动补齐
          </div>
        )}
        <form
          className={`composer ${disabled ? 'composer-disabled' : ''}`}
          onSubmit={(event) => {
            event.preventDefault()
            if (!disabled && input.trim()) onSend()
          }}
        >
          <textarea
            ref={inputRef}
            aria-label="向 CAD 助手发送消息"
            value={input}
            disabled={disabled}
            placeholder={
              !drawing
                ? '上传图纸，开始对话…'
                : historical
                  ? '回到当前版本后继续对话…'
                  : busy
                    ? '助手正在处理当前任务…'
                    : '询问图纸信息，或描述需要的修改…'
            }
            onChange={(event) => onInput(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
                event.preventDefault()
                if (!disabled && input.trim()) onSend()
              }
            }}
          />
          <div className="composer-toolbar">
            <span>
              <Sparkles size={12} />
              CAD 智能助手
            </span>
            {busy ? (
              <button className="stop-button" type="button" onClick={onStop} disabled={stopping}>
                <Square size={12} fill="currentColor" />
                {stopping ? '停止中' : '停止'}
              </button>
            ) : (
              <button
                className="send-button"
                type="submit"
                aria-label="发送消息"
                disabled={disabled || !input.trim()}
              >
                <ArrowUp size={18} />
              </button>
            )}
          </div>
        </form>
        <div className="composer-footnote">
          <span>
            {drawing?.current_revision
              ? `修改作用于当前版本 ${revisionLabel(drawing.current_revision)}`
              : '支持 DWG / DXF 格式'}
          </span>
          <span>
            Shift + Enter 换行
            <ArrowDown size={10} />
          </span>
        </div>
      </div>
    </section>
  )
}
