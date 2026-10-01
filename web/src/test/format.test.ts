import { describe, expect, it } from 'vitest'
import { relativeTime, toMillis } from '../format'

const NOW = Date.parse('2026-10-01T12:00:00Z')
const s = (ms: number) => ms / 1000

describe('relativeTime', () => {
  it('covers every bucket from epoch seconds', () => {
    expect(relativeTime(s(NOW - 10_000), NOW)).toBe('방금 전')
    expect(relativeTime(s(NOW - 5 * 60_000), NOW)).toBe('5분 전')
    expect(relativeTime(s(NOW - 3 * 3_600_000), NOW)).toBe('3시간 전')
    expect(relativeTime(s(NOW - 2 * 86_400_000), NOW)).toBe('2일 전')
    expect(relativeTime(s(NOW - 7 * 86_400_000), NOW)).toBe('7일 전')
  })

  it('switches to YYYY-MM-DD past 7 days', () => {
    const ms = NOW - 10 * 86_400_000
    const d = new Date(ms)
    const expected = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
    expect(relativeTime(s(ms), NOW)).toBe(expected)
  })

  it('accepts ISO strings', () => {
    expect(relativeTime('2026-10-01T11:30:00Z', NOW)).toBe('30분 전')
    expect(relativeTime('2026-10-01T11:30:00+00:00', NOW)).toBe('30분 전')
    expect(toMillis('2026-10-01T12:00:00Z')).toBe(NOW)
  })

  it('treats a clock slightly ahead as just now', () => {
    expect(relativeTime(s(NOW + 5_000), NOW)).toBe('방금 전')
  })
})
