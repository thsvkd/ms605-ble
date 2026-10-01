import { describe, expect, it } from 'vitest'
import { METER_TICK, METER_WIDTH, type MeterModel, meter, meterPlain } from '../meter'
import cases from './meter_cases.json'

// The same file tests/test_gui_meter_parity.py reads: both implementations must agree on every case.
interface Case {
  value: number
  threshold: number
  width: number
  tick_at: number
  plain: string
  over: boolean
}

const over = (m: MeterModel, tick: number) => m.cells.slice(tick + 1).filter((c) => c === 'fill').length
const under = (m: MeterModel, tick: number) => m.cells.slice(0, tick).filter((c) => c === 'fill').length

describe('meter (port of ms605.cli._ui.meter)', () => {
  it('uses the CLI monitor geometry by default', () => {
    expect([METER_WIDTH, METER_TICK]).toEqual([16, 5])
    expect(meter(1, 1).cells).toHaveLength(16)
  })

  it('has at least 20 shared cases, with zero and negative thresholds', () => {
    const all = cases as Case[]
    expect(all.length).toBeGreaterThanOrEqual(20)
    expect(all.some((c) => c.threshold === 0)).toBe(true)
    expect(all.some((c) => c.threshold < 0)).toBe(true)
  })

  it.each(cases as Case[])('$value/$threshold (w=$width, tick=$tick_at) -> $plain', (c) => {
    const m = meter(c.value, c.threshold, c.width, c.tick_at)
    expect(meterPlain(m)).toBe(c.plain)
    expect(m.over).toBe(c.over)
  })

  it('puts the tick at a fixed column whatever the threshold', () => {
    for (const thr of [10, 55, 200]) {
      const m = meter(thr, thr, 20, 6)
      expect(m.cells[6]).toBe('tick')
      expect(m.cells).toHaveLength(20)
    }
    for (const thr of [-40, -1, 0, 1, 999]) expect(meter(0, thr).cells[METER_TICK]).toBe('tick')
  })

  it.each([60, 1, 0, -33])('crosses the tick iff value > threshold (thr %i)', (thr) => {
    const at = meter(thr, thr)
    expect([under(at, 5), over(at, 5)]).toEqual([5, 0])
    const above = meter(thr + 1, thr)
    expect(under(above, 5)).toBe(5)
    expect(over(above, 5)).toBeGreaterThanOrEqual(1)
    const below = meter(thr - 1, thr)
    expect(under(below, 5)).toBeLessThan(5)
    expect(over(below, 5)).toBe(0)
  })

  it('distinguishes near-threshold values', () => {
    const bars = [55, 59, 60, 70, 77].map((v) => meterPlain(meter(v, 60)))
    expect(bars[0]?.slice(0, 5)).not.toBe(bars[2]?.slice(0, 5))
    expect(bars[1]?.slice(0, 5)).not.toBe(bars[2]?.slice(0, 5))
    expect(over(meter(70, 60), 5)).toBeGreaterThanOrEqual(1)
    expect(over(meter(77, 60), 5)).toBeGreaterThan(over(meter(61, 60), 5))
  })

  it('shows a value above a negative threshold as over (red)', () => {
    for (const v of [-10, 0]) {
      const m = meter(v, -33)
      expect(under(m, 5)).toBe(5)
      expect(over(m, 5)).toBeGreaterThanOrEqual(1)
      expect(m.over).toBe(true)
    }
  })

  it('does not saturate small values on a zero threshold', () => {
    const n = over(meter(5, 0), 5)
    expect(n).toBeGreaterThanOrEqual(1)
    expect(n).toBeLessThan(10)
  })

  it('colours the fill by value vs threshold', () => {
    expect(meter(61, 60).over).toBe(true)
    expect(meter(60, 60).over).toBe(false)
    expect(meter(0, 0).over).toBe(false)
  })

  it('clamps out-of-range values', () => {
    expect(meterPlain(meter(999, 10))).toBe('█████┃██████████')
    expect(meterPlain(meter(-999, 10))).toBe('─────┃──────────')
  })

  it.each([
    [1, 0],
    [10, 0],
    [10, 9],
    [10, 10],
  ])('rejects tickAt outside the bar (width %i, tick %i)', (width, tickAt) => {
    expect(() => meter(1, 1, width, tickAt)).toThrow(RangeError)
  })
})
