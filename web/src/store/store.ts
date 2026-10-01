import { create } from 'zustand'
import type { ApplyItemView, ApplyJobView, BatchView, SensorView, SiteView } from '../api/types'
import {
  addWatch,
  type AppState,
  type ConnState,
  type IncomingMessage,
  initialState,
  reduce,
  removeWatch,
} from './reducer'

export { selectBatchTally } from '../calibration'

interface Actions {
  /** Feed one WS message through the reducer. Returns true when the caller must resync. */
  applyMessage: (msg: IncomingMessage) => boolean
  setConn: (conn: ConnState, failures?: number) => void
  setUnauthorized: () => void
  /** Ref-counted live subscription (useLiveWatch); api/ws.ts mirrors `watch` to the server. */
  /** 14.8.2 calls these `watch`/`unwatch`; renamed because `watch` is also the refcount field. */
  watchLive: (ids: readonly string[]) => void
  unwatchLive: (ids: readonly string[]) => void
}

export type Store = AppState & Actions

export const useStore = create<Store>()((set, get) => ({
  ...initialState,
  applyMessage: (msg) => {
    const { state, resync } = reduce(get(), msg)
    if (msg.type === 'snapshot') set({ ...state, conn: 'open', failures: 0 })
    else if (state !== get()) set(state)
    return resync
  },
  setConn: (conn, failures) => {
    if (get().conn === 'unauthorized') return
    set(failures === undefined ? { conn } : { conn, failures })
  },
  setUnauthorized: () => set({ conn: 'unauthorized' }),
  watchLive: (ids) => {
    if (ids.length) set(addWatch(get(), ids))
  },
  unwatchLive: (ids) => {
    if (ids.length) set(removeWatch(get(), ids))
  },
}))

/** Back to the initial data (tests). */
export function resetStore(patch: Partial<AppState> = {}): void {
  useStore.setState({ ...initialState, ...patch })
}

// -- selectors (pure; components memoise them over the slices they read) ------------------

type Sensors = Pick<AppState, 'sensors'>

export interface Counts {
  connected: number
  lost: number
  offline: number
  total: number
}

export function selectCounts({ sensors }: Sensors): Counts {
  const counts: Counts = { connected: 0, lost: 0, offline: 0, total: 0 }
  for (const s of Object.values(sensors)) {
    counts.total += 1
    const link = s.live?.link
    if (link === 'connected') counts.connected += 1
    else if (link === 'lost') counts.lost += 1
    else if (link === undefined || link === 'disconnected') counts.offline += 1
  }
  return counts
}

export function selectUnregistered({ sensors }: Sensors): SensorView[] {
  return Object.values(sensors)
    .filter((s) => s.registry === null && s.live !== null)
    .sort((a, b) => (b.live?.gathered_at ?? 0) - (a.live?.gathered_at ?? 0))
}

export interface SiteGroup {
  site: SiteView
  sensors: SensorView[]
}

const ko = (a: string, b: string) => a.localeCompare(b, 'ko', { numeric: true })

/** Registered sensors grouped by site: site name, then alias (Korean collation). */
export function selectBySite({ sensors, sites }: Pick<AppState, 'sensors' | 'sites'>): SiteGroup[] {
  const groups = new Map<string, SiteGroup>()
  for (const s of Object.values(sensors)) {
    if (s.registry === null) continue
    const id = s.registry.site_id
    let group = groups.get(id)
    if (!group) {
      group = { site: sites[id] ?? { site_id: id, name: id }, sensors: [] }
      groups.set(id, group)
    }
    group.sensors.push(s)
  }
  const out = [...groups.values()]
  for (const g of out) g.sensors.sort((a, b) => ko(a.registry?.alias ?? '', b.registry?.alias ?? ''))
  return out.sort((a, b) => ko(a.site.name, b.site.name))
}

/** Sensors with a session in this run, newest gathered first. */
export function selectGathered({ sensors }: Sensors): SensorView[] {
  return Object.values(sensors)
    .filter((s) => s.live !== null)
    .sort((a, b) => (b.live?.gathered_at ?? 0) - (a.live?.gathered_at ?? 0))
}

/** Sensors with a session: by site name then alias, unregistered ones last (pickers, 14.8.5). */
export function selectSessions({ sensors, sites }: Pick<AppState, 'sensors' | 'sites'>): SensorView[] {
  const siteName = (s: SensorView) => (s.registry ? (sites[s.registry.site_id]?.name ?? s.registry.site_id) : null)
  return Object.values(sensors)
    .filter((s) => s.live !== null)
    .sort((a, b) => {
      const sa = siteName(a)
      const sb = siteName(b)
      if (sa === null || sb === null) {
        if (sa !== sb) return sa === null ? 1 : -1
        return ko(sensorName(a), sensorName(b))
      }
      return ko(sa, sb) || ko(sensorName(a), sensorName(b))
    })
}

/** Alias, else BLE name, else address, else id. */
export function sensorName(s: SensorView | undefined, fallback = ''): string {
  return s?.registry?.alias ?? s?.live?.name ?? s?.live?.address ?? (s?.device_id || fallback)
}

/** The current round is waiting or running. */
export function selectBatchActive({ batch }: Pick<AppState, 'batch'>): boolean {
  return batch !== null && (batch.state === 'waiting' || batch.state === 'running')
}

const NO_MEMBERS: ReadonlySet<string> = new Set()

/** The sensors of the active round (operation lock, G22); empty when nothing runs. */
export function selectBatchMembers({ batch }: { batch: BatchView | null }): ReadonlySet<string> {
  return selectBatchActive({ batch }) && batch ? new Set(batch.round_ids) : NO_MEMBERS
}

export function selectSites({ sites }: Pick<AppState, 'sites'>): SiteView[] {
  return Object.values(sites).sort((a, b) => ko(a.name, b.name))
}

// -- M4 (docs/GUI_API.md 15.9.4) ----------------------------------------------------------

/** An apply job is running (G25: one at a time, server-wide). */
export function selectApplyActive({ apply }: Pick<AppState, 'apply'>): boolean {
  return apply?.state === 'running'
}

/** The sensors of the running apply job (G27); empty when nothing runs. */
export function selectApplyMembers({ apply }: Pick<AppState, 'apply'>): ReadonlySet<string> {
  return apply?.state === 'running' ? new Set(apply.items.map((i) => i.device_id)) : NO_MEMBERS
}

/** That sensor's item in the current (running or last) job. */
export function selectApplyItem({ apply }: Pick<AppState, 'apply'>, id: string): ApplyItemView | undefined {
  return apply?.items.find((i) => i.device_id === id)
}

export interface ApplyTally {
  total: number
  ended: number
  verified: number
  partial: number
  unverified: number
  failed: number
}

export function selectApplyTally(job: ApplyJobView): ApplyTally {
  const out: ApplyTally = { total: job.items.length, ended: 0, verified: 0, partial: 0, unverified: 0, failed: 0 }
  for (const i of job.items) {
    if (i.state === 'queued' || i.state === 'applying') continue
    out.ended += 1
    out[i.state] += 1
  }
  return out
}
