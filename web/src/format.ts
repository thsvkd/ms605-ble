import { t } from './strings'

/** Epoch seconds (events) or an ISO 8601 string (registry/storage) -> epoch milliseconds. */
export function toMillis(value: number | string): number {
  return typeof value === 'number' ? value * 1000 : Date.parse(value)
}

function ymd(ms: number): string {
  const d = new Date(ms)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
}

/** 방금 전 / N분 전 / N시간 전 / N일 전, and YYYY-MM-DD past 7 days. */
export function relativeTime(value: number | string, now: number = Date.now()): string {
  const ms = toMillis(value)
  if (Number.isNaN(ms)) return String(value)
  const sec = Math.max(0, (now - ms) / 1000)
  if (sec < 60) return t.time.justNow
  const min = Math.floor(sec / 60)
  if (min < 60) return t.time.minutes(min)
  const hours = Math.floor(min / 60)
  if (hours < 24) return t.time.hours(hours)
  const days = Math.floor(hours / 24)
  if (days <= 7) return t.time.days(days)
  return ymd(ms)
}

export function formatLux(lux: number | null): string {
  return lux === null ? t.detail.none : `${lux} lx`
}
