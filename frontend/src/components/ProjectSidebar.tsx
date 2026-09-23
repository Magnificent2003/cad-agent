import { useState } from 'react'
import { Folder, FolderOpen, Layers3, PanelLeftClose, Pencil, Plus, X } from 'lucide-react'
import type { Project } from '../api/types'

interface Props {
  projects: Project[]
  selected: string | null
  collapsed: boolean
  disabled: boolean
  onCollapse: () => void
  onSelect: (id: string) => void
  onCreate: (name: string) => Promise<void>
  onRename: (id: string, name: string) => Promise<void>
}

export default function ProjectSidebar(props: Props) {
  const [editing, setEditing] = useState<string | null>(null)
  const [name, setName] = useState('')
  const [saving, setSaving] = useState(false)
  const edit = (project?: Project) => {
    setEditing(project?.id || 'new')
    setName(project?.name || '')
  }
  return (
    <aside
      className={`project-sidebar ${props.collapsed ? 'is-collapsed' : ''}`}
      aria-label="工程项目栏"
    >
      <div className="sidebar-heading">
        <span className="brand">
          <span className="brand-mark">
            <Layers3 size={22} />
          </span>
          <span>
            CAD <span className="brand-light">Agent</span>
          </span>
        </span>
        <button
          className="icon-button"
          onClick={props.onCollapse}
          aria-label="收起项目栏"
          title="收起项目栏"
        >
          <PanelLeftClose size={18} />
        </button>
      </div>
      <button className="button new-project" disabled={props.disabled} onClick={() => edit()}>
        <Plus size={17} />
        新建项目
      </button>
      <div className="sidebar-section-label">
        工程项目 <span>{props.projects.length}</span>
      </div>
      <nav className="project-list" aria-label="选择工程项目">
        {props.projects.map((project) => (
          <div
            className={`project-row ${project.id === props.selected ? 'is-selected' : ''}`}
            key={project.id}
          >
            <button
              disabled={props.disabled}
              className="project-item"
              onClick={() => props.onSelect(project.id)}
              aria-current={project.id === props.selected ? 'page' : undefined}
              title={project.name}
            >
              {project.id === props.selected ? <FolderOpen size={18} /> : <Folder size={18} />}
              <span>{project.name}</span>
            </button>
            <button
              className="icon-button project-rename"
              disabled={props.disabled}
              onClick={() => edit(project)}
              aria-label={`重命名项目 ${project.name}`}
              title="重命名"
            >
              <Pencil size={13} />
            </button>
          </div>
        ))}
        {!props.projects.length && <p className="sidebar-empty">新建项目，开始整理图纸。</p>}
      </nav>
      <div className="sidebar-footer">
        <Layers3 size={16} />
        <span>
          一个项目，多份图纸
          <br />
          <small>原图与每次成果集中保存</small>
        </span>
      </div>
      {editing && (
        <div className="dialog-backdrop" onClick={() => !saving && setEditing(null)}>
          <form
            className="project-dialog"
            role="dialog"
            aria-modal="true"
            aria-labelledby="project-dialog-title"
            onClick={(event) => event.stopPropagation()}
            onKeyDown={(event) => {
              if (event.key === 'Escape' && !saving) {
                event.stopPropagation()
                setEditing(null)
              }
            }}
            onSubmit={(event) => {
              event.preventDefault()
              if (!name.trim() || saving) return
              setSaving(true)
              void (
                editing === 'new'
                  ? props.onCreate(name.trim())
                  : props.onRename(editing, name.trim())
              )
                .then(() => setEditing(null))
                .catch(() => undefined)
                .finally(() => setSaving(false))
            }}
          >
            <div className="dialog-heading">
              <h2 id="project-dialog-title">{editing === 'new' ? '新建工程项目' : '重命名项目'}</h2>
              <button
                type="button"
                className="icon-button"
                onClick={() => setEditing(null)}
                disabled={saving}
                aria-label="关闭项目窗口"
              >
                <X size={18} />
              </button>
            </div>
            <label htmlFor="project-name">项目名称</label>
            <input
              id="project-name"
              autoFocus
              maxLength={120}
              value={name}
              onChange={(event) => setName(event.target.value)}
              placeholder="例如：白桥一号园（二期）"
            />
            <p>同一工程的原图、提取图纸和修改成果会保存在一起。</p>
            <div className="dialog-actions">
              <button
                type="button"
                className="button"
                disabled={saving}
                onClick={() => setEditing(null)}
              >
                取消
              </button>
              <button className="button button-primary" disabled={saving || !name.trim()}>
                {saving ? '保存中…' : '保存项目'}
              </button>
            </div>
          </form>
        </div>
      )}
    </aside>
  )
}
