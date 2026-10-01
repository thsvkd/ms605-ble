// Drafts: what the operator has changed but not applied (docs/GUI_API.md 15.9.2). Pure: tested directly.
// Nothing here talks to the server; the server computes the diff from the device (G24).
import type { ConfigView, DraftIn, DraftPreview, ProfileView, Section, SensorEdit } from './api/types'

export const THRESHOLD_UI_MAX = 500 // schemas.THRESHOLD_UI_MAX
export const ZONES = 7
export const SUBSENSORS = 3

export type ThresholdPart = 'trigger' | 'maintain'

/** One sensor: only what changed, the rest null. Thresholds are absolute (G32). */
export interface Edit {
  sensitivity: number | null
  /** 7; once one zone changes, all 7 are sent */
  zone_enable: boolean[] | null
  trigger: (number | null)[]
  maintain: (number | null)[]
  /** 3, each sorted */
  subsensor_zones: number[][] | null
  subsensor_timing: [number, number][] | null
  subsensor_enable: boolean[] | null
  dnd: boolean | null
}

export interface SensorDraft {
  deviceId: string
  base: ConfigView
  edit: Edit
}

/** Bulk edit (G32: relative by default). */
export interface BulkDraft {
  ids: string[]
  mode: 'relative' | 'absolute'
  /** 7: relative ±n (0 means unchanged), absolute a value */
  trigger: (number | null)[]
  maintain: (number | null)[]
  sensitivity: number | null
  /** null = unchanged, else all 7, the same for every sensor */
  zone_enable: boolean[] | null
  dnd: boolean | null
  absoluteAck: boolean
}

export interface CloneDraft {
  source: string | null
  sections: Section[]
  ids: string[]
  ack: boolean
}

export const SECTION_ORDER: readonly Section[] = [
  'sensitivity',
  'detect_mode',
  'zone_enable',
  'zone_thresholds',
  'subsensor_zones',
  'subsensor_timing',
  'subsensor_enable',
  'dnd',
]

export const CLONE_DEFAULT_SECTIONS: Section[] = ['sensitivity', 'zone_enable', 'zone_thresholds']

const nulls = (n: number): null[] => Array.from({ length: n }, () => null)

export function emptyEdit(): Edit {
  return {
    sensitivity: null,
    zone_enable: null,
    trigger: nulls(ZONES),
    maintain: nulls(ZONES),
    subsensor_zones: null,
    subsensor_timing: null,
    subsensor_enable: null,
    dnd: null,
  }
}

export function emptyBulk(ids: string[] = []): BulkDraft {
  return {
    ids,
    mode: 'relative',
    trigger: nulls(ZONES),
    maintain: nulls(ZONES),
    sensitivity: null,
    zone_enable: null,
    dnd: null,
    absoluteAck: false,
  }
}

/** The edit values gone, the selection and mode kept (after a 202, G37). */
export function clearBulkValues(b: BulkDraft): BulkDraft {
  return { ...emptyBulk(b.ids), mode: b.mode }
}

export function baseThreshold(d: SensorDraft, zone: number, part: ThresholdPart): number {
  return d.base.profile.zone_thresholds[zone]?.[part] ?? 0
}

/** The draft value: the edit, else the device's. */
export function draftThreshold(d: SensorDraft, zone: number, part: ThresholdPart): number {
  return d.edit[part][zone] ?? baseThreshold(d, zone, part)
}

type SectionKey = Exclude<keyof Edit, 'trigger' | 'maintain'>

/** The device's value of an edit key (dnd may be null: not readable). */
export function baseSection<K extends SectionKey>(d: SensorDraft, key: K): ProfileView[K] {
  return d.base.profile[key]
}

/** Any other section: the edit, else the device's. */
export function draftSection<K extends SectionKey>(d: SensorDraft, key: K): NonNullable<Edit[K]> | ProfileView[K] {
  return (d.edit[key] ?? d.base.profile[key]) as NonNullable<Edit[K]> | ProfileView[K]
}

/** Math.round, then [0, max(THRESHOLD_UI_MAX, base)]. */
export function clampThreshold(v: number, base: number): number {
  const max = Math.max(THRESHOLD_UI_MAX, base)
  return Math.min(max, Math.max(0, Math.round(v)))
}

export function setThreshold(d: SensorDraft, zone: number, part: ThresholdPart, v: number): SensorDraft {
  const base = baseThreshold(d, zone, part)
  const value = Math.round(v) === base ? base : clampThreshold(v, base)
  const cells = [...d.edit[part]]
  cells[zone] = value === base ? null : value
  return { ...d, edit: { ...d.edit, [part]: cells } }
}

const sortZones = (zones: number[][]): number[][] => zones.map((z) => [...new Set(z)].sort((a, b) => a - b))

function same(a: unknown, b: unknown): boolean {
  return JSON.stringify(a) === JSON.stringify(b)
}

export function setSection<K extends SectionKey>(d: SensorDraft, key: K, v: Edit[K]): SensorDraft {
  let value = v
  if (key === 'subsensor_zones' && v !== null) value = sortZones(v as number[][]) as Edit[K]
  const base = d.base.profile[key]
  const cmp = key === 'subsensor_zones' && base !== null ? sortZones(base as number[][]) : base
  return { ...d, edit: { ...d.edit, [key]: value === null || same(value, cmp) ? null : value } }
}

/** Differing elements of two arrays (per zone / sub-sensor). */
function diffCount(a: readonly unknown[] | null, b: readonly unknown[]): number {
  if (a === null) return 0
  return a.reduce<number>((n, v, i) => n + (same(v, b[i]) ? 0 : 1), 0)
}

/** Changed cells, in the same unit as DiffPreview rows (zone, sub-sensor, scalar). */
export function changedCount(d: SensorDraft): number {
  const e = d.edit
  const p = d.base.profile
  const timing = e.subsensor_timing
    ? e.subsensor_timing.reduce(
        (n, [pr, ab], i) => n + (pr === p.subsensor_timing[i]?.[0] ? 0 : 1) + (ab === p.subsensor_timing[i]?.[1] ? 0 : 1),
        0,
      )
    : 0
  return (
    (e.sensitivity === null ? 0 : 1) +
    diffCount(e.zone_enable, p.zone_enable) +
    e.trigger.filter((v) => v !== null).length +
    e.maintain.filter((v) => v !== null).length +
    diffCount(e.subsensor_zones, sortZones(p.subsensor_zones)) +
    timing +
    diffCount(e.subsensor_enable, p.subsensor_enable) +
    (e.dnd === null ? 0 : 1)
  )
}

export function isDirty(e: Edit): boolean {
  return (
    e.sensitivity !== null ||
    e.zone_enable !== null ||
    e.trigger.some((v) => v !== null) ||
    e.maintain.some((v) => v !== null) ||
    e.subsensor_zones !== null ||
    e.subsensor_timing !== null ||
    e.subsensor_enable !== null ||
    e.dnd !== null
  )
}

/** Every key is sent (15.4: generated request types make them required); thresholds are absolute. */
export function toSensorEdit(e: Edit): SensorEdit {
  const thresholds = e.trigger.some((v) => v !== null) || e.maintain.some((v) => v !== null)
  return {
    sensitivity: e.sensitivity,
    zone_enable: e.zone_enable,
    zone_thresholds: thresholds ? { mode: 'absolute', trigger: [...e.trigger], maintain: [...e.maintain] } : null,
    subsensor_zones: e.subsensor_zones,
    subsensor_timing: e.subsensor_timing,
    subsensor_enable: e.subsensor_enable,
    dnd: e.dnd,
  }
}

export function toDraftIn(d: SensorDraft): DraftIn {
  return { targets: [d.deviceId], changes: toSensorEdit(d.edit), expect_rev: null }
}

/**
 * Only the elements the edit changed (against its old base) laid onto the new base, so an element the
 * operator never touched takes the device's current value. `depth` 2 goes into pairs (timing fields).
 */
function carry(edit: readonly unknown[], was: readonly unknown[], now: readonly unknown[], depth = 1): unknown[] {
  return now.map((n, i) => {
    const e = edit[i]
    const w = was[i]
    if (same(e, w)) return n
    return depth > 1 && Array.isArray(e) && Array.isArray(w) && Array.isArray(n) ? carry(e, w, n, depth - 1) : e
  })
}

/** Keep each edit that still differs from the new base (a value equal to it drops out). */
export function rebaseDraft(d: SensorDraft, base: ConfigView): SensorDraft {
  let out: SensorDraft = { deviceId: d.deviceId, base, edit: emptyEdit() }
  for (const part of ['trigger', 'maintain'] as const) {
    d.edit[part].forEach((v, zone) => {
      if (v !== null) out = setThreshold(out, zone, part, v)
    })
  }
  out = setSection(out, 'sensitivity', d.edit.sensitivity)
  const was = d.base.profile
  const now = base.profile
  const arrays = [
    ['zone_enable', was.zone_enable, now.zone_enable, 1],
    ['subsensor_zones', sortZones(was.subsensor_zones), sortZones(now.subsensor_zones), 1],
    ['subsensor_timing', was.subsensor_timing, now.subsensor_timing, 2],
    ['subsensor_enable', was.subsensor_enable, now.subsensor_enable, 1],
  ] as const
  for (const [key, w, n, depth] of arrays) {
    const e = d.edit[key]
    out = setSection(out, key, (e === null ? null : carry(e, w, n, depth)) as Edit[typeof key])
  }
  // DND can only be edited where the device answered; a base without it drops the edit
  if (base.profile.dnd !== null) out = setSection(out, 'dnd', d.edit.dnd)
  return out
}

const relativeCells = (cells: (number | null)[], mode: BulkDraft['mode']) =>
  cells.map((v) => (mode === 'relative' && v === 0 ? null : v))

/** The request for a bulk draft, or null when it changes nothing. */
export function bulkToDraftIn(b: BulkDraft): DraftIn | null {
  const trigger = relativeCells(b.trigger, b.mode)
  const maintain = relativeCells(b.maintain, b.mode)
  const thresholds = trigger.some((v) => v !== null) || maintain.some((v) => v !== null)
  if (!thresholds && b.sensitivity === null && b.zone_enable === null && b.dnd === null) return null
  return {
    targets: [...b.ids],
    changes: {
      sensitivity: b.sensitivity,
      zone_enable: b.zone_enable === null ? null : [...b.zone_enable],
      zone_thresholds: thresholds ? { mode: b.mode, trigger, maintain } : null,
      subsensor_zones: null,
      subsensor_timing: null,
      subsensor_enable: null,
      dnd: b.dnd,
    },
    expect_rev: null,
  }
}

export function bulkIsDirty(b: BulkDraft): boolean {
  return bulkToDraftIn(b) !== null
}

/** Rows a bulk draft would change per sensor (zones and scalars), for the guard's count. */
export function bulkChangedCount(b: BulkDraft): number {
  const req = bulkToDraftIn(b)
  if (!req) return 0
  const c = req.changes
  const cells = c.zone_thresholds
    ? [...c.zone_thresholds.trigger, ...c.zone_thresholds.maintain].filter((v) => v !== null).length
    : 0
  return cells + (c.sensitivity === null ? 0 : 1) + (c.zone_enable === null ? 0 : 1) + (c.dnd === null ? 0 : 1)
}

/** Clears the values and the acknowledgement: +5 and 5 mean different things. */
export function setBulkMode(b: BulkDraft, mode: BulkDraft['mode']): BulkDraft {
  return { ...b, mode, trigger: nulls(ZONES), maintain: nulls(ZONES), absoluteAck: false }
}

/** The apply request's expect_rev: the config_rev each sensor had at the preview (G28). */
export function expectRevOf(p: DraftPreview): Record<string, number> {
  return Object.fromEntries(p.items.map((i) => [i.device_id, i.config_rev]))
}

/** Pointer x -> value on a linear axis: round(clamp((x - left) / width, 0, 1) × axisMax), then [0, max]. */
export function valueAt(clientX: number, rect: { left: number; width: number }, axisMax: number, max: number): number {
  const ratio = rect.width > 0 ? Math.min(1, Math.max(0, (clientX - rect.left) / rect.width)) : 0
  return Math.min(max, Math.max(0, Math.round(ratio * axisMax)))
}

/** m = 1.25 × max(base, value, live); the smallest of 100, 200, 500 that holds m, else ceil(m / 100) × 100. */
export function axisFor(base: number, value: number, live: number | null): number {
  const m = 1.25 * Math.max(base, value, live ?? 0)
  for (const step of [100, 200, 500]) if (m <= step) return step
  return Math.ceil(m / 100) * 100
}

/** 15.9.5 key table: a delta, an end, or null for a key the slider does not take. */
export function keyStep(key: string, shift: boolean): number | 'min' | 'max' | null {
  switch (key) {
    case 'ArrowRight':
    case 'ArrowUp':
      return shift ? 10 : 1
    case 'ArrowLeft':
    case 'ArrowDown':
      return shift ? -10 : -1
    case 'PageUp':
      return 10
    case 'PageDown':
      return -10
    case 'Home':
      return 'min'
    case 'End':
      return 'max'
    default:
      return null
  }
}
