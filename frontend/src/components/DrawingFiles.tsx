import { useState } from 'react'
import { Download, Files, LoaderCircle } from 'lucide-react'
import { readableError, saveBlob } from '../api/client'
import type { Artifact, ProjectVersion } from '../api/types'

interface Props {
  artifacts: Artifact[]
  versions: ProjectVersion[]
  onView: (key: string) => void
  downloadable: boolean
  downloading: string
  onDownload: (format: 'png' | 'dxf' | 'dwg') => void
}

export default function DrawingFiles(props: Props) {
  const [pendingFile, setPendingFile] = useState('')
  const [error, setError] = useState('')
  const download = async (file: Artifact) => {
    if (pendingFile) return
    setPendingFile(file.id)
    setError('')
    try {
      const response = await fetch(file.download_url)
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(
          typeof body.detail === 'string' ? body.detail : '文件下载失败，请稍后重试。',
        )
      }
      saveBlob(await response.blob(), file.name.split('/').at(-1) || file.name)
    } catch (cause) {
      setError(readableError(cause))
    } finally {
      setPendingFile('')
    }
  }
  return (
    <div className="drawing-downloads">
      <details
        className="project-files"
        onKeyDown={(event) => {
          if (event.key === 'Escape') event.currentTarget.open = false
        }}
      >
        <summary className="button" aria-label="项目生成文件">
          <Files size={14} />
          <span>文件</span>
          <span className="file-count">{props.artifacts.length}</span>
        </summary>
        <section className="files-popover" aria-label="项目生成文件列表">
          <div className="files-popover-heading">
            <strong>项目生成文件</strong>
            <span>图纸、图片与报告</span>
          </div>
          {error && (
            <p className="file-error" role="alert">
              {error}
            </p>
          )}
          {!props.artifacts.length && (
            <p className="files-empty">
              助手生成的文件会保存在这里。图纸预览就绪后会自动显示在右侧。
            </p>
          )}
          {[...props.artifacts].reverse().map((file) => {
            const version = props.versions.find(
              (item) =>
                item.drawing_id === file.drawing_id && item.revision_id === item.current_revision,
            )
            const pending = ['queued', 'running'].includes(file.preview_status)
            return (
              <div className="project-file" key={file.id}>
                <strong title={file.name}>{file.name}</strong>
                <small>
                  {Math.max(1, Math.ceil(file.bytes / 1024))} KB ·{' '}
                  {pending
                    ? '正在生成预览'
                    : version
                      ? version.label.split('_')[0]
                      : file.drawing_id
                        ? '预览不可用，可下载文件'
                        : '可下载'}
                </small>
                {file.preview_error && (
                  <p className="file-error">{readableError(new Error(file.preview_error))}</p>
                )}
                <div>
                  {version && (
                    <button
                      onClick={(event) => {
                        props.onView(version.key)
                        event.currentTarget.closest('details')!.open = false
                      }}
                    >
                      查看图纸
                    </button>
                  )}
                  <a
                    href={file.download_url}
                    download={file.name}
                    aria-label={`下载 ${file.name}`}
                    aria-disabled={Boolean(pendingFile)}
                    onClick={(event) => {
                      event.preventDefault()
                      void download(file)
                    }}
                  >
                    {pendingFile === file.id ? (
                      <LoaderCircle size={12} className="spin" />
                    ) : (
                      <Download size={12} />
                    )}
                    下载文件
                  </a>
                </div>
              </div>
            )
          })}
        </section>
      </details>
      {(['png', 'dxf', 'dwg'] as const).map((format) => (
        <button
          key={format}
          className={`button ${format === 'dxf' ? 'button-primary' : ''}`}
          disabled={!props.downloadable || Boolean(props.downloading)}
          onClick={() => props.onDownload(format)}
          aria-label={`下载 ${format.toUpperCase()}`}
          title={`下载当前查看图纸的 ${format.toUpperCase()}`}
        >
          {props.downloading === format ? (
            <LoaderCircle size={13} className="spin" />
          ) : (
            <Download size={13} />
          )}{' '}
          {format.toUpperCase()}
        </button>
      ))}
    </div>
  )
}
