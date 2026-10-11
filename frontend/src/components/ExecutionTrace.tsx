import { useEffect, useState } from 'react'
import { Check, ChevronRight, CircleAlert, Clock3, LoaderCircle, Wrench } from 'lucide-react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { readableError } from '../api/client'
import type { Message, MessagePart } from '../api/types'

const toolNames: Record<string, string> = {
  execute_code_headless: '执行无界面图纸脚本',
  execute_code: '执行图纸脚本',
  cad_inspect: '核实图纸数据',
  cad_layout: '布置设施与道路',
  cad_review: '复核实际成果',
  get_async_status: '检查执行状态',
  get_view: '查看图纸',
  screenshot: '查看图纸',
  get_objects: '检查图纸对象',
  get_object: '读取对象属性',
  get_rpc_status: '检查图纸服务',
  get_documents: '查看已打开文档',
  create_document: '创建图纸文档',
}

function duration(milliseconds: number) {
  const seconds = Math.max(0, Math.round(milliseconds / 1000))
  return seconds < 60 ? `${seconds} 秒` : `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`
}

function failed(part: MessagePart) {
  return part.state?.outcome === 'error' || part.state?.status === 'error'
}

function ToolStep({ part, active }: { part: MessagePart; active: boolean }) {
  const state = part.state
  const name =
    Object.entries(toolNames).find(([key]) => part.tool?.includes(key))?.[1] || '处理图纸任务'
  const error = failed(part)
  const returned = state?.status === 'completed' || state?.outcome === 'returned'
  const pending = !error && !returned
  const elapsed =
    state?.time?.start != null && state.time.end != null
      ? duration(state.time.end - state.time.start)
      : ''
  const status = error ? '需处理' : returned ? '已返回' : active ? '执行中' : '未完成'
  return (
    <div className={`execution-step ${error ? 'execution-step-error' : ''}`}>
      <details className="tool-part">
        <summary>
          {error ? (
            <CircleAlert size={14} />
          ) : returned ? (
            <Check size={14} />
          ) : active ? (
            <LoaderCircle size={14} className="spin" />
          ) : (
            <Wrench size={14} />
          )}
          <span>{name}</span>
          <span className="tool-status">
            {elapsed && `${elapsed} · `}
            {status}
          </span>
          <ChevronRight size={13} />
        </summary>
        <div className="tool-feedback">
          {state?.details ? <pre>{state.details}</pre> : <p>{state?.error || '暂无更多反馈。'}</p>}
        </div>
      </details>
      <p className="execution-step-summary">
        {state?.summary ||
          (error
            ? state?.error || '这次操作未成功，等待处理。'
            : pending
              ? active
                ? '正在等待工具反馈。'
                : '本轮已结束，未收到完成反馈。'
              : '工具已返回，结果由助手继续核对。')}
      </p>
    </div>
  )
}

// Consecutive assistant messages are one task, including tool retries and public updates.
// Internal reasoning is intentionally absent from this API; this is an execution record.
export default function ExecutionTrace({
  messages,
  active,
}: {
  messages: Message[]
  active: boolean
}) {
  const [open, setOpen] = useState(active)
  const [clock, setClock] = useState(Date.now())
  useEffect(() => {
    setOpen(active)
  }, [active])
  useEffect(() => {
    if (!active) return
    setClock(Date.now())
    const timer = window.setInterval(() => setClock(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [active])
  const last = messages.at(-1)
  const final =
    !active && last?.parts.some((part) => part.type === 'text' && part.text) ? last : undefined
  const steps = messages.flatMap((message) =>
    message.parts
      .filter(
        (part) => part.type === 'tool' || (part.type === 'text' && part.text && message !== final),
      )
      .map((part, index) => ({ part, key: `${message.id}:${part.id || index}` })),
  )
  const tools = steps.filter(({ part }) => part.type === 'tool')
  const errors = tools.filter(({ part }) => failed(part)).length
  const start = tools.map(({ part }) => part.state?.time?.start).find((value) => value != null)
  const end = Math.max(
    0,
    ...tools.map(({ part }) => part.state?.time?.end || 0),
    ...messages.map((message) => message.completed_at || 0),
  )
  const elapsed =
    start != null && (active || end >= start) ? duration((active ? clock : end) - start) : ''
  const latest = tools.at(-1)?.part
  const headline = !latest
    ? '正在分析当前任务…'
    : failed(latest)
      ? '正在处理工具反馈中的问题…'
      : latest.state?.status === 'completed'
        ? '正在核对结果并准备下一步…'
        : latest.state?.summary || '正在执行图纸操作…'
  return (
    <>
      {(steps.length > 0 || active) && (
        <section
          className={`execution-trace ${active ? 'execution-trace-active' : ''}`}
          aria-label="本轮处理过程"
        >
          <button
            className="execution-toggle"
            type="button"
            aria-expanded={open}
            onClick={() => setOpen(!open)}
          >
            {active ? <LoaderCircle size={15} className="spin" /> : <Clock3 size={15} />}
            <strong>{active ? '正在处理' : '处理过程'}</strong>
            <span>
              {tools.length} 次操作{errors > 0 && ` · ${errors} 次异常`}
              {elapsed && ` · ${elapsed}`}
            </span>
            <ChevronRight size={15} className={open ? 'expanded' : ''} />
          </button>
          {open && (
            <div className="execution-content">
              {steps.map(({ part, key }) =>
                part.type === 'tool' ? (
                  <ToolStep part={part} active={active} key={key} />
                ) : (
                  <div className="markdown execution-update" key={key}>
                    <ReactMarkdown remarkPlugins={[remarkGfm]}>{part.text}</ReactMarkdown>
                  </div>
                ),
              )}
              {active && (
                <p className="execution-current" role="status">
                  {headline}
                </p>
              )}
            </div>
          )}
        </section>
      )}
      {final?.parts
        .filter((part) => part.type === 'text' && part.text)
        .map((part, index) => (
          <div className="markdown" key={part.id || index}>
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{part.text}</ReactMarkdown>
          </div>
        ))}
      {messages
        .filter((message) => message.error)
        .map((message) => (
          <p className="message-error" key={message.id}>
            <CircleAlert size={13} />
            {readableError(new Error(message.error!))}
          </p>
        ))}
    </>
  )
}
