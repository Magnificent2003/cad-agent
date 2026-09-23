import type {
  ArtifactList,
  Drawing,
  Job,
  Message,
  Project,
  Revision,
  Session,
  Workbench,
} from './types'

export class ApiError extends Error {
  constructor(
    message: string,
    public status: number,
  ) {
    super(message)
  }
}

export async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(`/api${path}`, options)
  if (!response.ok) {
    let detail = '操作未完成，请稍后重试。'
    try {
      const body = await response.json()
      if (typeof body.detail === 'string') detail = body.detail
      else if (typeof body.error === 'string') detail = body.error
      else if (body.detail?.message) detail = body.detail.message
    } catch {
      /* The server may return a non-JSON gateway error. */
    }
    throw new ApiError(detail, response.status)
  }
  if (response.status === 204) return undefined as T
  return response.json() as Promise<T>
}

const enc = encodeURIComponent
export const api = {
  projects: () => request<Project[]>('/projects'),
  createProject: (name: string) =>
    request<Project>('/projects', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name }),
    }),
  renameProject: (id: string, name: string) =>
    request<Project>(`/projects/${enc(id)}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name }),
    }),
  workbench: (id: string) => request<Workbench>(`/projects/${enc(id)}/workbench`),
  artifacts: (session: string) => request<ArtifactList>(`/sessions/${enc(session)}/artifacts`),
  openArtifact: (id: string) =>
    request<{ drawing_id: string; session_id: string }>(`/artifacts/${enc(id)}/open`, {
      method: 'POST',
    }),
  health: () => request<Record<string, unknown>>('/health'),
  drawings: () => request<Drawing[]>('/drawings'),
  drawing: (id: string) => request<Drawing>(`/drawings/${enc(id)}`),
  revisions: (id: string) => request<Revision[]>(`/drawings/${enc(id)}/revisions`),
  sessions: (id: string) => request<Session[]>(`/drawings/${enc(id)}/sessions`),
  createSession: (id: string) =>
    request<Session>(`/drawings/${enc(id)}/sessions`, { method: 'POST' }),
  messages: (id: string) => request<Message[]>(`/sessions/${enc(id)}/messages`),
  sessionStatus: (id: string) => request<{ status: string }>(`/sessions/${enc(id)}/status`),
  send: (id: string, text: string, revision: string) =>
    request(`/sessions/${enc(id)}/messages`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text, expected_revision: revision }),
    }),
  abort: (id: string) =>
    request<{ status: string; model_stopped: boolean; jobs: Job[] }>(`/sessions/${enc(id)}/abort`, {
      method: 'POST',
    }),
  job: (id: string) => request<Job>(`/jobs/${enc(id)}`),
  upload: (file: File, projectId: string) => {
    const body = new FormData()
    body.append('file', file)
    return request<{ drawing_id: string; job_id: string }>(`/projects/${enc(projectId)}/drawings`, {
      method: 'POST',
      body,
    })
  },
  preview: (id: string, revision: string, layout: string, format: 'svg' | 'png') =>
    `/api/drawings/${enc(id)}/revisions/${enc(revision)}/preview?${new URLSearchParams({ layout, format, renderer: '2' })}`,
  download: (id: string, revision: string, layout: string, format: string) =>
    `/api/drawings/${enc(id)}/revisions/${enc(revision)}/download?${new URLSearchParams({ layout, format })}`,
}

export function readableError(error: unknown): string {
  if (error instanceof TypeError) return '暂时无法连接服务，请检查服务状态后重试。'
  if (error instanceof Error) {
    // Internal paths and credentials are never useful in the workbench UI.
    if (/Traceback|Bearer\s|sk-[\w-]+|\/(?:home|tmp|usr|var)\/|[A-Z]:\\/i.test(error.message)) {
      return '处理未完成，请查看服务诊断，或重试当前操作。'
    }
    return error.message
  }
  return '操作未完成，请稍后重试。'
}

export function revisionLabel(revision: string | null | undefined, number?: number): string {
  if (!revision) return '尚无版本'
  if (/^r\d+$/.test(revision)) return revision
  return number ? `r${String(number).padStart(4, '0')}` : revision.slice(0, 8)
}

export function saveBlob(blob: Blob, filename: string) {
  const objectUrl = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = objectUrl
  link.download = filename
  document.body.appendChild(link)
  link.click()
  link.remove()
  window.setTimeout(() => URL.revokeObjectURL(objectUrl), 1000)
}
