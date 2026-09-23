import { useEffect, useState } from 'react'
import {
  CircleAlert,
  FileUp,
  Focus,
  Info,
  Layers3,
  LoaderCircle,
  Minus,
  MousePointer2,
  Plus,
  RefreshCw,
  ScanLine,
  X,
} from 'lucide-react'
import { TransformComponent, TransformWrapper } from 'react-zoom-pan-pinch'
import { api, readableError } from '../api/client'
import { layoutLabel, metadataInfo } from '../api/metadata'
import DrawingFiles from './DrawingFiles'
import type { Artifact, Drawing, Metadata, ProjectVersion } from '../api/types'

interface Props {
  drawing: Drawing | null
  versions: ProjectVersion[]
  selectedKey: string
  currentKey: string
  onVersion: (key: string) => void
  historical: boolean
  onCurrent: () => void
  artifacts: Artifact[]
  downloading: string
  onDownload: (format: 'png' | 'dxf' | 'dwg') => void
  revision: string
  layout: string
  onLayout: (layout: string) => void
  processing: boolean
  onUpload: () => void
  metadata: Metadata
}

export default function DrawingViewer({
  drawing,
  versions,
  selectedKey,
  currentKey,
  onVersion,
  historical,
  onCurrent,
  artifacts,
  downloading,
  onDownload,
  revision,
  layout,
  onLayout,
  processing,
  onUpload,
  metadata,
}: Props) {
  const [preview, setPreview] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [format, setFormat] = useState<'svg' | 'png'>('svg')
  const [reload, setReload] = useState(0)
  const [showInfo, setShowInfo] = useState(false)
  const [scale, setScale] = useState(1)
  const info = metadataInfo(metadata)
  const selected = versions.find((item) => item.key === selectedKey)

  useEffect(() => {
    setFormat('svg')
  }, [drawing?.id, revision, layout])

  useEffect(() => {
    setPreview('')
    setError('')
    if (!drawing?.id || !revision) {
      setLoading(false)
      return
    }
    const controller = new AbortController()
    let objectUrl = ''
    setLoading(true)
    const load = async () => {
      for (let attempt = 0; attempt < 120; attempt++) {
        const response = await fetch(api.preview(drawing.id, revision, layout, format), {
          signal: controller.signal,
        })
        if (response.status === 202) {
          await new Promise((resolve) => window.setTimeout(resolve, 1500))
          if (controller.signal.aborted) return
          continue
        }
        if (!response.ok) {
          if (format === 'svg') {
            setFormat('png')
            return
          }
          throw new Error('当前布局的预览暂不可用，可以重试或下载图纸查看。')
        }
        objectUrl = URL.createObjectURL(await response.blob())
        if (controller.signal.aborted) {
          URL.revokeObjectURL(objectUrl)
          return
        }
        setPreview(objectUrl)
        setLoading(false)
        return
      }
      throw new Error('预览生成时间较长，请稍后重试。')
    }
    void load().catch((cause) => {
      if (controller.signal.aborted) return
      setError(readableError(cause))
      setLoading(false)
    })
    return () => {
      controller.abort()
      if (objectUrl) URL.revokeObjectURL(objectUrl)
    }
  }, [drawing?.id, revision, layout, format, reload])

  return (
    <section className="viewer-panel" aria-label="图纸预览">
      <div className="panel-heading viewer-heading">
        <div className="panel-title">
          <ScanLine size={17} />
          <span>图纸预览</span>
          <span className="quiet-tag">只读</span>
        </div>
        <label className="version-picker">
          <span className="visually-hidden">项目图纸与历史</span>
          <select
            aria-label="项目图纸与历史"
            disabled={!versions.length}
            value={selectedKey}
            onChange={(event) => onVersion(event.target.value)}
          >
            {!versions.length && <option value="">尚无图纸</option>}
            {[...versions].reverse().map((item) => (
              <option key={item.key} value={item.key}>
                {item.label}
                {item.key === currentKey ? ' · 当前' : ''}
              </option>
            ))}
          </select>
        </label>
      </div>
      <div className="viewer-toolbar">
        <label className="select-wrap">
          <Layers3 size={14} />
          <select
            aria-label="图纸布局"
            disabled={!revision}
            value={layout}
            onChange={(event) => onLayout(event.target.value)}
          >
            {info.layouts.map((name) => (
              <option key={name} value={name}>
                {layoutLabel(name)}
              </option>
            ))}
          </select>
        </label>
        <DrawingFiles
          artifacts={artifacts}
          versions={versions}
          onView={onVersion}
          downloadable={Boolean(revision)}
          downloading={downloading}
          onDownload={onDownload}
        />
      </div>
      <div className="drawing-canvas">
        {drawing && revision && (
          <select
            className="canvas-format-select"
            aria-label="预览格式"
            value={format}
            onChange={(event) => setFormat(event.target.value as 'svg' | 'png')}
          >
            <option value="svg">SVG 矢量</option>
            <option value="png">PNG 概览</option>
          </select>
        )}
        {drawing && revision && (
          <div className="canvas-title">
            <span className="canvas-status-dot" />
            {selected?.label || drawing.name}
            <span className="canvas-title-divider" />
            {layoutLabel(layout)}
          </div>
        )}
        {historical && (
          <div className="canvas-notice history-notice">
            正在查看历史版本
            <button onClick={onCurrent}>回到当前版本</button>
          </div>
        )}
        {processing && drawing && revision && (
          <div className="canvas-notice processing-notice">
            <LoaderCircle className="spin" size={14} />
            正在处理，完成后将更新图纸
          </div>
        )}
        <TransformWrapper
          key={`${drawing?.id}-${revision}-${layout}`}
          minScale={0.25}
          maxScale={20}
          initialScale={1}
          centerOnInit
          wheel={{ step: 0.15 }}
          onTransformed={(_, state) => setScale(state.scale)}
        >
          {({ zoomIn, zoomOut, resetTransform }) => (
            <>
              <TransformComponent
                wrapperClass="drawing-transform"
                contentClass="drawing-transform-content"
              >
                {preview && (
                  <img
                    className="drawing-image"
                    src={preview}
                    alt={`${drawing?.name || '图纸'} · ${layoutLabel(layout)} · ${selected?.label || ''}`}
                    draggable={false}
                    onError={() => {
                      if (format === 'svg') setFormat('png')
                      else {
                        setPreview('')
                        setError('预览无法显示，请重试。')
                      }
                    }}
                  />
                )}
              </TransformComponent>
              {preview && (
                <div className="zoom-toolbar">
                  <button
                    className="icon-button"
                    title="缩小"
                    aria-label="缩小"
                    onClick={() => zoomOut()}
                  >
                    <Minus size={17} />
                  </button>
                  <span>{Math.round(scale * 100)}%</span>
                  <button
                    className="icon-button"
                    title="放大"
                    aria-label="放大"
                    onClick={() => zoomIn()}
                  >
                    <Plus size={17} />
                  </button>
                  <span className="toolbar-divider" />
                  <button
                    className="icon-button"
                    title="适应窗口"
                    aria-label="适应窗口"
                    onClick={() => resetTransform()}
                  >
                    <Focus size={18} />
                  </button>
                </div>
              )}
            </>
          )}
        </TransformWrapper>
        {!drawing && !processing && (
          <div className="canvas-empty">
            <div className="blueprint-icon">
              <ScanLine size={42} strokeWidth={1.2} />
            </div>
            <h2>从一张图纸开始</h2>
            <p>
              上传施工图，查看结构、提取信息，
              <br />
              通过对话完成有记录的图纸修改。
            </p>
            <button className="button canvas-upload" onClick={onUpload}>
              <FileUp size={16} />
              选择 DWG / DXF 图纸
            </button>
            <span className="canvas-empty-hint">保留原始图纸 · 每次修改独立保存</span>
          </div>
        )}
        {(loading || (processing && !revision)) && (
          <div className="canvas-empty">
            <LoaderCircle size={30} className="spin" />
            <h2>{revision ? '正在生成图纸预览' : '正在读取图纸'}</h2>
            <p>复杂图纸需要一些时间，请稍候。</p>
          </div>
        )}
        {!loading && error && (
          <div className="canvas-empty canvas-error">
            <CircleAlert size={32} />
            <h2>预览暂不可用</h2>
            <p>{error}</p>
            <button
              className="button canvas-upload"
              onClick={() => setReload((value) => value + 1)}
            >
              <RefreshCw size={15} />
              重新加载
            </button>
          </div>
        )}
        {drawing && !revision && !processing && !loading && (
          <div className="canvas-empty">
            <CircleAlert size={32} />
            <h2>{drawing.status === 'failed' ? '图纸导入未完成' : '暂无可用预览'}</h2>
            <p>请检查导入结果，或重新上传图纸。</p>
          </div>
        )}
        {preview && (
          <div className="canvas-legend">
            <MousePointer2 size={13} />
            <span>拖动平移 · 滚轮缩放</span>
            {format === 'png' && <span className="png-tag">PNG 预览</span>}
          </div>
        )}
        {drawing && revision && (
          <button
            className={`canvas-info-button ${showInfo ? 'active' : ''}`}
            onClick={() => setShowInfo((value) => !value)}
            aria-label="图纸信息"
            title="图纸信息"
          >
            <Info size={18} />
            {info.warnings.length > 0 && <i />}
          </button>
        )}
        {showInfo && drawing && (
          <aside className="drawing-info">
            <div className="drawing-info-heading">
              <strong>图纸信息</strong>
              <button
                className="icon-button"
                onClick={() => setShowInfo(false)}
                aria-label="关闭图纸信息"
              >
                <X size={16} />
              </button>
            </div>
            <dl>
              <div>
                <dt>单位</dt>
                <dd>{info.unit}</dd>
              </div>
              <div>
                <dt>图层</dt>
                <dd>{info.layerCount}</dd>
              </div>
              <div>
                <dt>图元</dt>
                <dd>{info.entityCount.toLocaleString()}</dd>
              </div>
              <div>
                <dt>布局</dt>
                <dd>{info.layouts.length}</dd>
              </div>
            </dl>
            {Object.keys(info.counts).length > 0 && (
              <details>
                <summary>图元类型统计</summary>
                <dl>
                  {Object.entries(info.counts)
                    .sort((a, b) => b[1] - a[1])
                    .map(([name, count]) => (
                      <div key={name}>
                        <dt>{name}</dt>
                        <dd>{count.toLocaleString()}</dd>
                      </div>
                    ))}
                </dl>
              </details>
            )}
            {info.warnings.length > 0 && (
              <div className="drawing-warnings">
                <strong>
                  <CircleAlert size={14} />
                  导入与渲染提示
                </strong>
                {info.warnings.map((warning, index) => (
                  <p key={index}>{readableError(new Error(warning))}</p>
                ))}
              </div>
            )}
            <p className="info-footnote">预览用于辅助看图，请结合原图核对文字与专业对象。</p>
          </aside>
        )}
      </div>
    </section>
  )
}
