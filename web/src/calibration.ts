// Job / batch / preflight state -> on-screen text (docs/GUI_API.md 14.8.8-14.8.9). Pure: tested directly.
import {
  AlertTriangle,
  CheckCircle2,
  CircleSlash,
  Clock,
  HelpCircle,
  Hourglass,
  Loader2,
  UserRound,
  XCircle,
} from 'lucide-react'
import type { BatchState, BatchView, CalibrationJobView, CalibrationState, LinkState, PresenceView } from './api/types'
import type { SensorStatus } from './status'
import { t } from './strings'

export type Status = SensorStatus

export interface JobContext {
  batchState: BatchState
  gathering: boolean
  /** The sensor's link right now; null when it has no session. */
  link: LinkState | null
}

const ENDED: ReadonlySet<CalibrationState> = new Set(['succeeded', 'failed', 'lost', 'timeout', 'cancelled'])
const FAILED: ReadonlySet<CalibrationState> = new Set(['failed', 'lost', 'timeout'])

export const isEnded = (state: CalibrationState) => ENDED.has(state)
export const isFailed = (state: CalibrationState) => FAILED.has(state)

function lostHint(ctx: JobContext): string {
  if (ctx.batchState === 'running') return t.retry.whenDone
  if (ctx.link === 'connected') return t.retry.reconnected
  return ctx.gathering ? t.retry.pressAgain : t.retry.pressAgainGather
}

/** 14.8.9 job table: the first row that matches. */
export function jobStatus(job: CalibrationJobView, ctx: JobContext): Status {
  switch (job.state) {
    case 'idle':
      return ctx.batchState === 'running'
        ? { kind: 'progress', icon: Loader2, label: t.job.preparing, spin: true }
        : { kind: 'off', icon: Clock, label: t.job.idle }
    case 'starting':
      return { kind: 'progress', icon: Loader2, label: t.job.starting, spin: true }
    case 'learning':
      return { kind: 'progress', icon: Hourglass, label: t.job.learning, hint: t.job.learningHint }
    case 'succeeded':
      if (job.detail.startsWith('반영값 재조회 실패'))
        return { kind: 'warn', icon: CheckCircle2, label: t.job.noReadback, hint: t.job.noReadbackHint }
      if (job.detail.startsWith('결과 저장 실패'))
        return { kind: 'warn', icon: CheckCircle2, label: t.job.notSaved, hint: t.job.notSavedHint }
      return {
        kind: 'ok',
        icon: CheckCircle2,
        label: t.job.succeeded,
        ...(job.history_saved ? { hint: t.job.saved } : {}),
      }
    case 'failed':
      if (job.error?.startsWith('busy: '))
        return { kind: 'warn', icon: AlertTriangle, label: t.job.busy, hint: t.job.busyHint }
      return {
        kind: 'danger',
        icon: XCircle,
        label: t.job.failed,
        hint: job.error === null ? t.job.deviceFailed : t.job.error(job.error),
      }
    case 'lost':
      return {
        kind: 'warn',
        icon: AlertTriangle,
        label: job.started ? t.job.lostLearning : t.job.lostBefore,
        hint: lostHint(ctx),
      }
    case 'timeout':
      return { kind: 'danger', icon: Clock, label: t.job.timeout, hint: t.job.timeoutHint }
    case 'cancelled':
      return job.started
        ? { kind: 'off', icon: CircleSlash, label: t.job.cancelled, hint: t.job.cancelledHint }
        : { kind: 'off', icon: CircleSlash, label: t.job.cancelled }
  }
}

/** 14.8.9 preflight table. */
export function presenceStatus(p: PresenceView): Status {
  if (p.occupied === true) {
    const reasons = [p.presence === true && t.preflight.bySub, p.pir === true && t.preflight.byPir].filter(Boolean)
    return {
      kind: 'warn',
      icon: UserRound,
      label: t.preflight.occupied,
      ...(reasons.length ? { hint: t.preflight.because(reasons.join('·')) } : {}),
    }
  }
  if (p.occupied === false) return { kind: 'ok', icon: CheckCircle2, label: t.preflight.empty }
  if (p.error !== null) return { kind: 'off', icon: HelpCircle, label: t.preflight.unknown, hint: p.error }
  return { kind: 'off', icon: HelpCircle, label: t.preflight.unknown, hint: t.preflight.noSamples }
}

/** Elapsed vs the expected duration (G21): an estimate, never the device's own progress. */
export function jobProgress(job: CalibrationJobView, expectedS: number): { ratio: number | null; overdue: boolean } {
  if (job.state === 'succeeded') return { ratio: 1, overdue: false }
  if (job.state !== 'learning') return { ratio: null, overdue: false }
  const elapsed = job.elapsed_s ?? 0
  const ratio = expectedS > 0 ? Math.min(elapsed / expectedS, 0.99) : 0
  return { ratio, overdue: elapsed > expectedS }
}

const pad2 = (n: number) => String(n).padStart(2, '0')

function clock(total: number): string {
  const h = Math.floor(total / 3600)
  const m = Math.floor((total % 3600) / 60)
  const s = total % 60
  return h > 0 ? `${h}:${pad2(m)}:${pad2(s)}` : `${m}:${pad2(s)}`
}

/** Rounded up, so the screen never shows 0:00 while time is left: 59.2 -> "1:00", 3600 -> "1:00:00". */
export const formatCountdown = (s: number) => clock(Math.max(0, Math.ceil(s)))

/** Rounded down: 72.9 -> "1:12". */
export const formatElapsed = (s: number) => clock(Math.max(0, Math.floor(s)))

export function formatHHMM(d: Date): string {
  return `${pad2(d.getHours())}:${pad2(d.getMinutes())}`
}

/** `2시간 3분` / `12분` / `1분 미만`. */
export function formatRemaining(s: number): string {
  const minutes = Math.floor(Math.max(0, s) / 60)
  if (minutes < 1) return t.batch.remainLt1
  const h = Math.floor(minutes / 60)
  return h > 0 ? t.batch.remainH(h, minutes % 60) : t.batch.remainM(minutes)
}

/** HH:MM -> today if that is still ahead of `now`, else tomorrow (CLI resolve_target_datetime). */
export function resolveAt(hhmm: string, now: Date): Date {
  const [h, m] = hhmm.split(':').map(Number)
  const at = new Date(now)
  at.setHours(h ?? 0, m ?? 0, 0, 0)
  if (at.getTime() <= now.getTime()) at.setDate(at.getDate() + 1)
  return at
}

export interface Tally {
  ended: number
  total: number
  succeeded: number
  failed: number
}

function tally(jobs: CalibrationJobView[]): Tally {
  const out: Tally = { ended: 0, total: jobs.length, succeeded: 0, failed: 0 }
  for (const j of jobs) {
    if (isEnded(j.state)) out.ended += 1
    if (j.state === 'succeeded') out.succeeded += 1
    if (isFailed(j.state)) out.failed += 1
  }
  return out
}

/** Counted over the current round's sensors (`round_ids`). */
export function selectBatchTally(batch: BatchView): Tally {
  const round = new Set(batch.round_ids)
  return tally(batch.jobs.filter((j) => round.has(j.device_id)))
}

/** Counted over every sensor of the batch (the results headline). */
export const batchTotals = (batch: BatchView): Tally => tally(batch.jobs)

/** 14.8.9 batch headline. `remainingS` is the countdown (server clock); null falls back to the local clock. */
export function batchHeadline(batch: BatchView, remainingS: number | null, now: Date): string {
  switch (batch.state) {
    case 'waiting': {
      const left = remainingS ?? Math.max(0, batch.fire_at - now.getTime() / 1000)
      if (batch.start === 'now') return t.batch.startingNow
      if (batch.start === 'delay') return t.batch.countdown(formatCountdown(left))
      return t.batch.scheduled(formatHHMM(new Date(batch.fire_at * 1000)), formatRemaining(left))
    }
    case 'running': {
      const r = selectBatchTally(batch)
      return t.batch.running(r.ended, r.total)
    }
    case 'done': {
      const all = batchTotals(batch)
      return t.batch.done(all.succeeded, all.failed)
    }
    case 'cancelled':
      return t.batch.cancelled
  }
}

/** `70 → 64 (−6)`: a real minus sign for decreases, `+` for increases, `(0)` when unchanged. */
export function compareCell(before: number, after: number): string {
  const d = after - before
  const delta = d > 0 ? `+${d}` : d < 0 ? `−${-d}` : '0'
  return t.compare.cell(before, after, delta)
}

/** G38: a cancel this close to the fire time may reach the server once the round runs (core 5.3). */
export const CANCEL_CONFIRM_WITHIN_S = 5

/** running -> true; waiting -> remainingS unknown or under 5 s; anything else false. */
export function needsCancelConfirm(batch: BatchView, remainingS: number | null): boolean {
  if (batch.state === 'running') return true
  if (batch.state === 'waiting') return remainingS === null || remainingS < CANCEL_CONFIRM_WITHIN_S
  return false
}
