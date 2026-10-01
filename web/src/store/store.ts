import { create } from 'zustand'
import type { SensorView, SiteView } from '../api/types'
import { type AppState, type ConnState, type IncomingMessage, initialState, reduce } from './reducer'

interface Actions {
  /** Feed one WS message through the reducer. Returns true when the caller must resync. */
  applyMessage: (msg: IncomingMessage) => boolean
  setConn: (conn: ConnState, failures?: number) => void
  setUnauthorized: () => void
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

export function selectSites({ sites }: Pick<AppState, 'sites'>): SiteView[] {
  return Object.values(sites).sort((a, b) => ko(a.name, b.name))
}
