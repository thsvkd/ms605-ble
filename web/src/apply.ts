// Diff rows, risks and apply results -> on-screen text (docs/GUI_API.md 15.9.11, 15.9.16). Pure: tested directly.
import { AlertTriangle, CheckCircle2, Clock, HelpCircle, Loader2, XCircle } from 'lucide-react'
import type { ApplyItemView, ApplyJobView, ApplyKind, Change, DraftPreview, RiskCode, Section } from './api/types'
import type { Status } from './calibration'
import { selectApplyTally } from './store/store'
import { t } from './strings'

const MINUS = '−'

export function sectionLabel(s: Section): string {
  return t.section[s]
}

export const sectionList = (sections: readonly string[]) =>
  sections.map((s) => (s in t.section ? sectionLabel(s as Section) : s)).join(', ')

export function rowLabel(c: Change): string {
  const i = c.index ?? 0
  switch (c.section) {
    case 'sensitivity':
      return t.section.sensitivity
    case 'detect_mode':
      return t.section.detect_mode
    case 'dnd':
      return t.section.dnd
    case 'zone_enable':
      return t.common.zoneToggle(i)
    case 'zone_thresholds':
      return `Z${i} ${c.part === 'maintain' ? t.live.maintain : t.live.trigger}`
    case 'subsensor_zones':
      return `S${i + 1} ${t.adv.zones}`
    case 'subsensor_timing':
      return `S${i + 1} ${c.part === 'absence_s' ? t.adv.absence : t.adv.presence}`
    case 'subsensor_enable':
      return `S${i + 1} ${t.adv.use}`
  }
}

function valueText(c: Change, v: Change['before']): string {
  if (v === null) return t.common.unknown
  if (typeof v === 'boolean') return v ? t.edit.zoneOn : t.edit.zoneOff
  if (Array.isArray(v)) return v.length ? v.map((z) => `Z${z}`).join(', ') : t.adv.zonesNone
  if (c.section === 'sensitivity') return t.sens[v] ?? String(v)
  if (c.section === 'detect_mode') return t.mode[v] ?? String(v)
  if (c.section === 'subsensor_timing') return `${v}${t.adv.seconds}`
  return String(v)
}

/** `70 → 75 (+5)`, `켜짐 → 꺼짐`, `Z0, Z1 → 없음`, `5초 → 10초 (+5)`. */
export function formatChange(c: Change): string {
  const text = `${valueText(c, c.before)} → ${valueText(c, c.after)}`
  const numeric = c.section === 'zone_thresholds' || c.section === 'subsensor_timing'
  if (!numeric || typeof c.before !== 'number' || typeof c.after !== 'number') return text
  const d = c.after - c.before
  return `${text} (${d > 0 ? `+${d}` : d < 0 ? `${MINUS}${-d}` : '0'})`
}

export function riskText(code: RiskCode): string {
  return t.risk[code]
}

export const kindText = (kind: ApplyKind) => t.apply.kind[kind]

/** 15.9.16 apply-item table: the first row that matches. */
export function applyItemStatus(item: ApplyItemView, kind: ApplyKind): Status {
  switch (item.state) {
    case 'queued':
      return { kind: 'off', icon: Clock, label: t.apply.queued }
    case 'applying':
      return { kind: 'progress', icon: Loader2, label: t.apply.applying, hint: t.apply.applyingHint, spin: true }
    case 'verified':
      if (item.skipped.length)
        return { kind: 'ok', icon: CheckCircle2, label: t.apply.verified, hint: t.apply.skipped(sectionList(item.skipped)) }
      return { kind: 'ok', icon: CheckCircle2, label: kind === 'rollback' ? t.apply.rolledBack : t.apply.verified }
    case 'partial':
      return {
        kind: 'warn',
        icon: AlertTriangle,
        label: t.apply.partial,
        hint: t.apply.partialHint(sectionList(item.mismatched)),
      }
    case 'unverified':
      return { kind: 'warn', icon: HelpCircle, label: t.apply.unverified, hint: t.apply.unverifiedHint }
    case 'failed':
      if (item.error?.startsWith('busy: '))
        return { kind: 'warn', icon: AlertTriangle, label: t.apply.busy, hint: t.apply.busyHint }
      if (item.error === 'not connected')
        return { kind: 'warn', icon: AlertTriangle, label: t.apply.lost, hint: t.apply.lostHint }
      if (item.snapshot === null)
        return { kind: 'danger', icon: XCircle, label: t.apply.failedClean, hint: t.apply.error(item.error ?? '') }
      return {
        kind: 'danger',
        icon: XCircle,
        label: t.apply.failedWrite,
        hint: `${t.apply.failedWriteHint} · ${t.apply.error(item.error ?? '')}`,
      }
  }
}

/** 15.9.16 headline: counts that are 0 drop out, except 확인함. */
export function applyHeadline(job: ApplyJobView): string {
  const kind = kindText(job.kind)
  const n = selectApplyTally(job)
  if (job.state === 'running') return t.apply.running(kind, n.ended, n.total)
  if (n.verified === n.total) return t.apply.doneAll(kind, n.total)
  const counts = [
    t.apply.cVerified(n.verified),
    n.partial && t.apply.cPartial(n.partial),
    n.unverified && t.apply.cUnverified(n.unverified),
    n.failed && t.apply.cFailed(n.failed),
  ].filter(Boolean)
  return t.apply.done(kind, counts.join(' · '))
}

/** It reached the write (an automatic snapshot exists) and has ended. */
export function canRollback(item: ApplyItemView): boolean {
  return item.snapshot !== null && item.state !== 'queued' && item.state !== 'applying'
}

export function needsOverwriteAck(p: DraftPreview): boolean {
  return p.risks.includes('absolute_overwrite')
}

/** storage snapshot name `YYYYMMDDTHHMMSSffffffZ` -> epoch ms (NaN when it is not one). */
export function snapshotTime(name: string): number {
  const m = /^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})(\d{3})\d{3}Z$/.exec(name)
  if (!m) return Number.NaN
  const [, y, mo, d, h, mi, s, ms] = m.map(Number) as number[]
  return Date.UTC(y ?? 0, (mo ?? 1) - 1, d ?? 1, h ?? 0, mi ?? 0, s ?? 0, ms ?? 0)
}

const pad2 = (n: number) => String(n).padStart(2, '0')

/** Local `YYYY-MM-DD HH:MM` (history rows, rollback titles). */
export function formatDateTime(ms: number): string {
  if (Number.isNaN(ms)) return '—'
  const d = new Date(ms)
  return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())} ${pad2(d.getHours())}:${pad2(d.getMinutes())}`
}
