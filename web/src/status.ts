import { AlertTriangle, CheckCircle2, CircleSlash, Clock, Hourglass, Loader2, type LucideIcon } from 'lucide-react'
import type { SensorView } from './api/types'
import { relativeTime } from './format'
import { t } from './strings'

export type StatusKind = 'ok' | 'progress' | 'warn' | 'off'

export interface SensorStatus {
  kind: StatusKind
  icon: LucideIcon
  /** Never empty: the status is never carried by colour alone. */
  label: string
  hint?: string
  spin?: boolean
}

export function busyText(busy: string): string {
  switch (busy) {
    case 'identify':
      return t.busy.identify
    case 'read':
      return t.busy.read
    case 'apply':
      return t.busy.apply
    case 'calibration':
      return t.busy.calibration
    default:
      return busy
  }
}

/** The single source of every on-screen sensor status (docs/GUI_API.md 9.3). */
export function sensorStatus(view: SensorView, gathering: boolean, now: number = Date.now()): SensorStatus {
  const live = view.live
  if (live === null) {
    const seen = view.registry?.last_seen
    return {
      kind: 'off',
      icon: Clock,
      label: t.link.offline,
      hint: seen ? t.time.lastSeen(relativeTime(seen, now)) : t.time.neverSeen,
    }
  }
  switch (live.link) {
    case 'connected':
      if (live.busy) return { kind: 'progress', icon: Hourglass, label: t.busy.label(busyText(live.busy)) }
      return { kind: 'ok', icon: CheckCircle2, label: t.link.connected }
    case 'connecting':
      return { kind: 'progress', icon: Loader2, label: t.link.connecting, spin: true }
    case 'lost':
      return {
        kind: 'warn',
        icon: AlertTriangle,
        label: t.link.lost,
        hint: gathering ? t.link.lostHintGathering : t.link.lostHintIdle,
      }
    case 'disconnected':
      return { kind: 'off', icon: CircleSlash, label: t.link.disconnected }
  }
}
