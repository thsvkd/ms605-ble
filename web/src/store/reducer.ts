import type { GatherStatus, Notice, PendingView, SensorView, ServerInfo, ServerMessage, SiteView } from '../api/types'

export type ConnState = 'connecting' | 'open' | 'reconnecting' | 'unauthorized'

export interface AppState {
  conn: ConnState
  /** Consecutive failed reconnect attempts (picks the banner text). */
  failures: number
  lastSeq: number | null
  server: ServerInfo | null
  gather: GatherStatus
  sites: Record<string, SiteView>
  sensors: Record<string, SensorView>
  pending: PendingView[]
  /** Most recent last, at most NOTICE_LIMIT. */
  notices: Notice[]
}

/** A message type this client does not know yet (M3+ adds kinds, 6.7). */
export interface UnknownMessage {
  type: string
  seq: number | null
  ts?: number
  data?: unknown
}

export type IncomingMessage = ServerMessage | UnknownMessage

export const NOTICE_LIMIT = 20

export const initialState: AppState = {
  conn: 'connecting',
  failures: 0,
  lastSeq: null,
  server: null,
  gather: { gathering: false, connecting: [] },
  sites: {},
  sensors: {},
  pending: [],
  notices: [],
}

function byKey<T>(items: T[], key: (item: T) => string): Record<string, T> {
  const out: Record<string, T> = {}
  for (const item of items) out[key(item)] = item
  return out
}

/** Apply one message's payload (ordering already checked). */
function apply(state: AppState, msg: IncomingMessage): AppState {
  const known = msg as ServerMessage
  switch (known.type) {
    case 'snapshot': {
      const d = known.data
      return {
        ...state,
        server: d.server,
        gather: d.gather,
        sites: byKey(d.sites, (s) => s.site_id),
        sensors: byKey(d.sensors, (s) => s.device_id),
        pending: d.pending,
      }
    }
    case 'sensor':
      return { ...state, sensors: { ...state.sensors, [known.data.device_id]: known.data } }
    case 'sensor_removed': {
      if (!(known.data.device_id in state.sensors)) return state
      const sensors = { ...state.sensors }
      delete sensors[known.data.device_id]
      return { ...state, sensors }
    }
    case 'sites':
      return { ...state, sites: byKey(known.data.sites, (s) => s.site_id) }
    case 'pending':
      return { ...state, pending: known.data.pending }
    case 'gather':
      return { ...state, gather: known.data }
    case 'notice':
      return { ...state, notices: [...state.notices, known.data].slice(-NOTICE_LIMIT) }
    default:
      return state
  }
}

/**
 * The only way server state enters the client (G10). Ordering per docs/GUI_API.md 7.6:
 * a snapshot replaces everything; otherwise seq <= lastSeq is a duplicate, lastSeq + 1 applies,
 * and anything larger is a gap -> `resync` (the caller reconnects for a fresh snapshot).
 */
export function reduce(state: AppState, msg: IncomingMessage): { state: AppState; resync: boolean } {
  if (msg.type === 'snapshot') {
    const lastSeq = typeof msg.seq === 'number' ? msg.seq : null
    return { state: { ...apply(state, msg), lastSeq }, resync: false }
  }
  // unordered: null, or a malformed message without seq (which must not reach lastSeq)
  if (typeof msg.seq !== 'number') return { state: apply(state, msg), resync: false }
  if (state.lastSeq === null) return { state, resync: true }
  if (msg.seq <= state.lastSeq) return { state, resync: false }
  if (msg.seq > state.lastSeq + 1) return { state, resync: true }
  return { state: { ...apply(state, msg), lastSeq: msg.seq }, resync: false }
}
