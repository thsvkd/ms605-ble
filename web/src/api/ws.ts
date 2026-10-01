import { useStore } from '../store/store'
import type { IncomingMessage } from '../store/reducer'

/** The subset of the browser WebSocket this client uses (tests pass a fake). */
export interface SocketLike {
  readonly readyState: number
  onopen: ((ev: Event) => void) | null
  onmessage: ((ev: MessageEvent) => void) | null
  onclose: ((ev: CloseEvent) => void) | null
  onerror: ((ev: Event) => void) | null
  close(code?: number, reason?: string): void
}

export interface WsOptions {
  url?: string
  createSocket?: (url: string) => SocketLike
  /** 0..1, for the ±20% backoff jitter. */
  random?: () => number
}

export interface WsHandle {
  stop(): void
}

const OPEN = 1
const CONNECTING = 0
const BACKOFF_S = [0.5, 1, 2, 4, 5]
const AUTH_CLOSE = new Set([4401, 4403])
const TOO_SLOW = 1013

export function backoffMs(attempt: number, random: () => number = Math.random): number {
  const base = BACKOFF_S[Math.min(attempt, BACKOFF_S.length - 1)] ?? 5
  return base * 1000 * (0.8 + 0.4 * random())
}

function defaultUrl(): string {
  const scheme = location.protocol === 'https:' ? 'wss' : 'ws'
  return `${scheme}://${location.host}/ws`
}

/**
 * The single /ws connection (docs/GUI_API.md 7.6): every message goes through the store's reducer;
 * a seq gap or a 1013 reconnects at once, 4401/4403 stops for good, anything else backs off
 * 0.5, 1, 2, 4, 5, 5 … s and resets once a snapshot arrives.
 */
export function connectWs(options: WsOptions = {}): WsHandle {
  const url = options.url ?? defaultUrl()
  const createSocket = options.createSocket ?? ((u: string) => new WebSocket(u))
  const random = options.random ?? Math.random
  const store = useStore

  let socket: SocketLike | null = null
  let timer: ReturnType<typeof setTimeout> | null = null
  let attempt = 0
  let stopped = false

  const clearTimer = () => {
    if (timer !== null) clearTimeout(timer)
    timer = null
  }

  const detach = (s: SocketLike) => {
    s.onopen = s.onmessage = s.onclose = s.onerror = null
  }

  const open = () => {
    clearTimer()
    if (stopped) return
    if (socket) {
      // e.g. a visibility wake-up while the old socket is still CLOSING: never keep two sockets
      detach(socket)
      socket.close()
      socket = null
    }
    let gotSnapshot = false
    const s = createSocket(url)
    socket = s
    s.onmessage = (ev) => {
      let msg: IncomingMessage
      try {
        msg = JSON.parse(String(ev.data)) as IncomingMessage
      } catch {
        return
      }
      const resync = store.getState().applyMessage(msg)
      if (msg.type === 'snapshot') {
        gotSnapshot = true
        attempt = 0
      }
      if (resync) restartNow()
    }
    s.onclose = (ev) => {
      detach(s)
      if (socket !== s || stopped) return // a replaced socket's late close must not reconnect again
      socket = null
      if (AUTH_CLOSE.has(ev.code)) {
        store.getState().setUnauthorized()
        return
      }
      const failures = gotSnapshot ? 0 : store.getState().failures + 1
      store.getState().setConn('reconnecting', failures)
      if (ev.code === TOO_SLOW && gotSnapshot) {
        // fell behind after a snapshot: catch up at once; a socket closed before its snapshot backs off
        open()
        return
      }
      timer = setTimeout(open, backoffMs(attempt, random))
      attempt += 1
    }
  }

  /** Drop the current socket without waiting for its close event and reconnect for a snapshot. */
  const restartNow = () => {
    const s = socket
    if (s) {
      detach(s)
      socket = null
      s.close()
    }
    store.getState().setConn('reconnecting')
    open()
  }

  const onVisible = () => {
    if (document.visibilityState !== 'visible' || stopped) return
    if (store.getState().conn === 'unauthorized') return
    const state = socket?.readyState
    if (state === OPEN || state === CONNECTING) return
    open()
  }

  document.addEventListener('visibilitychange', onVisible)
  open()

  return {
    stop() {
      stopped = true
      clearTimer()
      document.removeEventListener('visibilitychange', onVisible)
      if (socket) {
        detach(socket)
        socket.close(1000)
        socket = null
      }
    },
  }
}
