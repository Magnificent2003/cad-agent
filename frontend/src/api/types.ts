export type Metadata = Record<string, unknown>

export interface Drawing {
  id: string
  name: string
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
