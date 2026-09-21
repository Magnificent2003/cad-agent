import { useCallback, useEffect, useRef, useState } from 'react'
import type { SessionEvent } from './types'

export function useSessionEvents(
  sessionId: string | null,
  onEvent: (event: SessionEvent) => void,
  onReconnect: () => void,
) {
  const [connected, setConnected] = useState(false)
  const current = useRef<{ id: string | null; connected: boolean }>({ id: null, connected: false })
  const eventCallback = useRef(onEvent)
  const reconnectCallback = useRef(onReconnect)
  eventCallback.current = onEvent
  reconnectCallback.current = onReconnect

  useEffect(() => {
    setConnected(false)
    current.current = { id: sessionId, connected: false }
    if (!sessionId) return
    const source = new EventSource(`/api/sessions/${encodeURIComponent(sessionId)}/events`)
    source.onopen = () => {
      current.current = { id: sessionId, connected: true }
      setConnected(true)
      reconnectCallback.current()
    }
    source.onerror = () => {
      current.current = { id: sessionId, connected: false }
      setConnected(false)
    }
    source.onmessage = ({ data }) => {
      try {
        eventCallback.current(JSON.parse(data) as SessionEvent)
      } catch {
        /* Ignore heartbeat payloads. */
      }
    }
    return () => {
      source.close()
      current.current = { id: null, connected: false }
    }
  }, [sessionId])

  const waitUntilConnected = useCallback(async (id: string) => {
    for (let i = 0; i < 100; i++) {
      if (current.current.id === id && current.current.connected) return
      await new Promise((resolve) => window.setTimeout(resolve, 100))
    }
    throw new Error('对话连接暂未建立，请稍后重试。')
  }, [])

  return { connected, waitUntilConnected }
}
