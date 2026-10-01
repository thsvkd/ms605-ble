// Per-client drafts (G30, D11): a Zustand store apart from the server-state store, which only WS changes (G10).
import type { NavigateOptions } from 'wouter'
import { create } from 'zustand'
import type { ConfigView } from '../api/types'
import {
  type BulkDraft,
  bulkChangedCount,
  bulkIsDirty,
  type CloneDraft,
  changedCount,
  emptyEdit,
  isDirty,
  rebaseDraft,
  type SensorDraft,
} from '../draft'

/** An apply job this client started from a scope; `settled` once its end has been acted on (15.9.2 table). */
export interface MyApply {
  applyId: string
  settled: boolean
}

export interface DraftsState {
  /** scope 'sensor:<id>' */
  sensors: Record<string, SensorDraft>
  /** scope 'bulk' */
  bulk: BulkDraft | null
  /** scope 'bulk' */
  clone: CloneDraft | null
  pendingNav: { to: string; options?: NavigateOptions; go: () => void } | null
  /** scope -> the job this client started there */
  mine: Record<string, MyApply>
  /** Already open (back and forth): kept as it is. */
  openSensor(base: ConfigView): void
  /** Only the base changes; edits now equal to it drop out. */
  rebase(base: ConfigView): void
  updateSensor(id: string, f: (d: SensorDraft) => SensorDraft): void
  setBulk(b: BulkDraft | null): void
  setClone(c: CloneDraft | null): void
  setMine(scope: string, mine: MyApply | null): void
  /** `forget`: a sensor draft goes away with its base, so the next visit reads the device again. */
  discard(scope: string, forget?: boolean): void
}

export const useDrafts = create<DraftsState>()((set, get) => ({
  sensors: {},
  bulk: null,
  clone: null,
  pendingNav: null,
  mine: {},
  openSensor: (base) => {
    if (get().sensors[base.device_id]) return
    set({ sensors: { ...get().sensors, [base.device_id]: { deviceId: base.device_id, base, edit: emptyEdit() } } })
  },
  rebase: (base) => {
    const d = get().sensors[base.device_id]
    const next = d ? rebaseDraft(d, base) : { deviceId: base.device_id, base, edit: emptyEdit() }
    set({ sensors: { ...get().sensors, [base.device_id]: next } })
  },
  updateSensor: (id, f) => {
    const d = get().sensors[id]
    if (d) set({ sensors: { ...get().sensors, [id]: f(d) } })
  },
  setBulk: (bulk) => set({ bulk }),
  setClone: (clone) => set({ clone }),
  setMine: (scope, mine) => {
    const next = { ...get().mine }
    if (mine) next[scope] = mine
    else delete next[scope]
    set({ mine: next })
  },
  discard: (scope, forget = false) => {
    if (scope === 'bulk') {
      set({ bulk: null, clone: null })
      return
    }
    const id = sensorOf(scope)
    const d = id === null ? undefined : get().sensors[id]
    if (id === null || !d) return
    const sensors = { ...get().sensors }
    if (forget) delete sensors[id]
    else sensors[id] = { ...d, edit: emptyEdit() }
    set({ sensors })
  },
}))

/** Back to empty (tests). */
export function resetDrafts(): void {
  useDrafts.setState({ sensors: {}, bulk: null, clone: null, pendingNav: null, mine: {} })
}

export const sensorScope = (id: string) => `sensor:${id}`

function sensorOf(scope: string): string | null {
  return scope.startsWith('sensor:') ? scope.slice('sensor:'.length) : null
}

/** `/sensors/<id>(/...)?` -> 'sensor:<id>', `/bulk` -> 'bulk', anything else null. Query and hash ignored. */
export function scopeOf(path: string): string | null {
  const p = path.split(/[?#]/, 1)[0] ?? ''
  const m = /^\/sensors\/([^/]+)(\/.*)?$/.exec(p)
  if (m?.[1]) return sensorScope(decodeURIComponent(m[1]))
  if (p === '/bulk' || p === '/bulk/') return 'bulk'
  return null
}

type Readable = Pick<DraftsState, 'sensors' | 'bulk' | 'clone'>

export function isScopeDirty(s: Readable, scope: string): boolean {
  if (scope === 'bulk') {
    const clone = s.clone
    return (s.bulk !== null && bulkIsDirty(s.bulk)) || (clone !== null && clone.source !== null && clone.ids.length > 0)
  }
  const id = sensorOf(scope)
  const d = id === null ? undefined : s.sensors[id]
  return d !== undefined && isDirty(d.edit)
}

/** What the guard says will be thrown away. */
export function scopeChangedCount(s: Readable, scope: string): number {
  if (scope === 'bulk') {
    const n = s.bulk ? bulkChangedCount(s.bulk) : 0
    return n || (s.clone?.source && s.clone.ids.length ? s.clone.sections.length : 0)
  }
  const id = sensorOf(scope)
  const d = id === null ? undefined : s.sensors[id]
  return d ? changedCount(d) : 0
}
