import type {
  ApplyJobView,
  BatchView,
  GatherStatus,
  LiveData,
  Notice,
  PendingView,
  SensorView,
  ServerInfo,
  ServerMessage,
  SiteView,
} from '../api/types'

export type ConnState = 'connecting' | 'open' | 'reconnecting' | 'unauthorized'

/** The server's countdown, anchored to the moment it arrived (14.8.2). */
export interface CountdownAnchor {
  batch_id: string
  /** Seconds left as the server measured them. */
  remaining_s: number
  /** Date.now() when that value arrived (ms). */
  received_at: number
}

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
  /** The server's current or last batch (G16). */
  batch: BatchView | null
  countdown: CountdownAnchor | null
  /** The server's running or last apply job (G25). */
  apply: ApplyJobView | null
  /** device_id -> latest live frame; only for watched sensors. */
  live: Record<string, LiveData>
  /** How many mounted views watch each sensor (client-side refcount; never from the server). */
  watch: Record<string, number>
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
  batch: null,
  countdown: null,
  apply: null,
  live: {},
  watch: {},
}

function byKey<T>(items: T[], key: (item: T) => string): Record<string, T> {
  const out: Record<string, T> = {}
  for (const item of items) out[key(item)] = item
  return out
}

/** A waiting batch's countdown from fire_at and the message's server timestamp (both server clock). */
function anchorFor(batch: BatchView | null, ts: number, now: number): CountdownAnchor | null {
  if (batch?.state !== 'waiting') return null
  return { batch_id: batch.batch_id, remaining_s: Math.max(0, batch.fire_at - ts), received_at: now }
}

/** Apply one message's payload (ordering already checked). */
function apply(state: AppState, msg: IncomingMessage, now: number): AppState {
  const known = msg as ServerMessage
  switch (known.type) {
    case 'snapshot': {
      const d = known.data
      const batch = d.batch ?? null
      return {
        ...state,
        server: d.server,
        gather: d.gather,
        sites: byKey(d.sites, (s) => s.site_id),
        sensors: byKey(d.sensors, (s) => s.device_id),
        pending: d.pending,
        batch,
        countdown: anchorFor(batch, known.ts, now),
        apply: d.apply ?? null,
        live: {},
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
    case 'batch':
      return { ...state, batch: known.data, countdown: anchorFor(known.data, known.ts, now) }
    case 'calibration_job': {
      const job = known.data
      const batch = state.batch
      if (batch?.batch_id !== job.batch_id) return state
      const jobs = batch.jobs.map((j) => (j.device_id === job.device_id ? job : j))
      return { ...state, batch: { ...batch, jobs } }
    }
    case 'apply':
      return { ...state, apply: known.data }
    case 'live': {
      const id = known.data.device_id
      if ((state.watch[id] ?? 0) <= 0) return state
      return { ...state, live: { ...state.live, [id]: known.data } }
    }
    case 'countdown': {
      const c = known.data
      if (state.batch?.batch_id !== c.batch_id || state.batch.state !== 'waiting') return state
      return { ...state, countdown: { batch_id: c.batch_id, remaining_s: c.remaining_s, received_at: now } }
    }
    default:
      return state
  }
}

/**
 * The only way server state enters the client (G10). Ordering per docs/GUI_API.md 7.6:
 * a snapshot replaces everything; otherwise seq <= lastSeq is a duplicate, lastSeq + 1 applies,
 * and anything larger is a gap -> `resync` (the caller reconnects for a fresh snapshot).
 */
export function reduce(
  state: AppState,
  msg: IncomingMessage,
  now: number = Date.now(),
): { state: AppState; resync: boolean } {
  if (msg.type === 'snapshot') {
    const lastSeq = typeof msg.seq === 'number' ? msg.seq : null
    return { state: { ...apply(state, msg, now), lastSeq }, resync: false }
  }
  // unordered: null (live, countdown), or a malformed message without seq (which must not reach lastSeq)
  if (typeof msg.seq !== 'number') return { state: apply(state, msg, now), resync: false }
  if (state.lastSeq === null) return { state, resync: true }
  if (msg.seq <= state.lastSeq) return { state, resync: false }
  if (msg.seq > state.lastSeq + 1) return { state, resync: true }
  return { state: { ...apply(state, msg, now), lastSeq: msg.seq }, resync: false }
}

/** One more view watches each id (14.8.2). */
export function addWatch(state: AppState, ids: readonly string[]): AppState {
  if (ids.length === 0) return state
  const watch = { ...state.watch }
  for (const id of ids) watch[id] = (watch[id] ?? 0) + 1
  return { ...state, watch }
}

/** One view fewer; at 0 the id leaves `watch` and its last frame leaves `live`. */
export function removeWatch(state: AppState, ids: readonly string[]): AppState {
  if (ids.length === 0) return state
  const watch = { ...state.watch }
  let live = state.live
  for (const id of ids) {
    const n = (watch[id] ?? 0) - 1
    if (n > 0) {
      watch[id] = n
      continue
    }
    delete watch[id]
    if (id in live) {
      if (live === state.live) live = { ...live }
      delete live[id]
    }
  }
  return { ...state, watch, live }
}
