import type { LiveData } from './api/types'

/** true / false, or null = no value yet. */
export type Tri = boolean | null

export type SignalKind = 'pir' | 'rf' | 'presence'

/**
 * What one live frame says about a room (docs/GUI_API.md 14.8.5.1).
 * - pir: live.pir as sent (null = the device has not reported it yet).
 * - rf: any ENABLED zone with the device's own trigger_active flag.
 * - present: the device's own call, any(sub_sensor_presence). Never derived on the host from pir and rf.
 * Without a frame every field is unknown.
 */
export interface Presence {
  pir: Tri
  rf: Tri
  present: Tri
  /** S1..S3 as sent; null without a frame. */
  subs: boolean[] | null
}

export const NO_PRESENCE: Presence = { pir: null, rf: null, present: null, subs: null }

export function presenceOf(frame: LiveData | undefined): Presence {
  if (!frame) return NO_PRESENCE
  return {
    pir: frame.pir,
    rf: frame.zones.some((z) => z.enabled && z.trigger_active),
    present: frame.sub_sensor_presence.some(Boolean),
    subs: frame.sub_sensor_presence,
  }
}

export type SignalTone = 'hit' | 'present' | 'off' | 'unknown'

/** Raw signals that fire are red ('hit'); the device's call is blue ('present'); no value is 'unknown'. */
export function signalTone(kind: SignalKind, value: Tri): SignalTone {
  if (value === null) return 'unknown'
  if (!value) return 'off'
  return kind === 'presence' ? 'present' : 'hit'
}

export interface PresenceTally {
  total: number
  present: number
  pir: number
  rf: number
  /** connected sensors whose 재실 call is unknown (no frame yet) */
  unknown: number
  /** sensors not connected now: their last frame is history, so it is not counted as a detection */
  offline: number
}

/** One watched sensor for the tally: its latest frame and whether its link is connected now. */
export interface TallyEntry {
  frame: LiveData | undefined
  connected: boolean
}

/** The monitor's summary bar: one count per signal over the watched sensors that are connected now. */
export function presenceTally(entries: readonly TallyEntry[]): PresenceTally {
  const out: PresenceTally = { total: entries.length, present: 0, pir: 0, rf: 0, unknown: 0, offline: 0 }
  for (const { frame, connected } of entries) {
    if (!connected) {
      out.offline += 1
      continue
    }
    const p = presenceOf(frame)
    if (p.present === null) out.unknown += 1
    if (p.present) out.present += 1
    if (p.pir) out.pir += 1
    if (p.rf) out.rf += 1
  }
  return out
}

/**
 * Sensors whose 재실 call flipped between two `live` maps, for the polite live region. Only a real flip
 * (재실 <-> 부재) counts: a first frame, a frame leaving on unwatch, or the same call again is not news.
 */
export function presenceFlips(
  prev: Readonly<Record<string, LiveData>>,
  next: Readonly<Record<string, LiveData>>,
): { id: string; present: boolean }[] {
  if (prev === next) return []
  const out: { id: string; present: boolean }[] = []
  for (const [id, frame] of Object.entries(next)) {
    const before = prev[id]
    if (!before || before === frame) continue
    const was = presenceOf(before).present
    const now = presenceOf(frame).present
    if (was !== null && now !== null && was !== now) out.push({ id, present: now })
  }
  return out
}
