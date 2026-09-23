export type Metadata = Record<string, unknown>

export interface Drawing {
  id: string
  name: string
  project_id: string
  current_revision: string | null
  metadata?: Metadata | null
  status?: string
}

export interface Revision {
  id: string
  number?: number
  created_at?: string
  summary?: string
  metadata?: Metadata
}

export interface Session {
  drawing_id?: string
  engine?: string
  id: string
  title: string
  status?: string
}

export interface MessagePart {
  id?: string
  type: string
  text?: string
  tool?: string
  state?: { status?: string; title?: string; error?: string }
}

export interface Message {
  id: string
  role: string
  parts: MessagePart[]
  created_at?: string
  error?: string | null
}

export interface Job {
  id: string
  status: string
  error?: string | null
  result?: Record<string, unknown> | null
}

export interface SessionEvent {
  type: string
  status?: string | { type?: string }
  error?: string | { message?: string }
  drawing_id?: string
  revision_id?: string
  [key: string]: unknown
}

export interface Artifact {
  id: string
  name: string
  bytes: number
  task_id: string
  drawing_id: string | null
  preview_status: string
  preview_error: string | null
  download_url: string
}
export interface ArtifactList {
  artifacts: Artifact[]
  task: { id: string; status: string; error: string | null } | null
}

export interface Project {
  id: string
  name: string
}

export interface ProjectVersion {
  key: string
  drawing_id: string
  revision_id: string
  current_revision: string
  name: string
  label: string
  number: number
  summary: string
  created_at: string
  artifact_id: string | null
}

export interface Workbench {
  project: Project
  versions: ProjectVersion[]
  sessions: Session[]
  artifacts: Artifact[]
}
