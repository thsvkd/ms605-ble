import { describe, expect, it } from 'vitest'
import {
  axisFor,
  type BulkDraft,
  bulkToDraftIn,
  changedCount,
  clampThreshold,
  emptyBulk,
  emptyEdit,
  expectRevOf,
  keyStep,
  rebaseDraft,
  type SensorDraft,
  setBulkMode,
  setSection,
  setThreshold,
  toDraftIn,
  toSensorEdit,
  valueAt,
} from '../draft'
import { scopeOf } from '../store/drafts'
import { configView, deviceId, previewRisky, profile } from './fixtures'

const N = null
const draft = (): SensorDraft => ({ deviceId: deviceId(1), base: configView(1), edit: emptyEdit() })
const rect = { left: 0, width: 200 }

describe('valueAt: pointer x -> value on the linear axis', () => {
  it('maps the ends, clamps outside the track and rounds', () => {
    expect(valueAt(0, rect, 100, 500)).toBe(0)
    expect(valueAt(200, rect, 100, 500)).toBe(100)
    expect(valueAt(-40, rect, 100, 500)).toBe(0)
    expect(valueAt(260, rect, 100, 500)).toBe(100)
    expect(valueAt(130, rect, 100, 500)).toBe(65)
    expect(valueAt(131.2, rect, 100, 500)).toBe(66)
  })

  it('never passes max', () => {
    expect(valueAt(200, rect, 600, 500)).toBe(500)
  })
})

describe('axisFor', () => {
  it('100 / 200 / 500, else the next 100 above 1.25×', () => {
    expect(axisFor(60, 64, 58)).toBe(100)
    expect(axisFor(90, 90, null)).toBe(200) // 112.5
    expect(axisFor(450, 450, null)).toBe(600) // 562.5 > 500
    expect(axisFor(600, 600, null)).toBe(800)
    expect(axisFor(60, 60, 380)).toBe(500) // a live value far above stays on the axis
  })
})

describe('keyStep', () => {
  it.each([
    ['ArrowRight', false, 1],
    ['ArrowUp', false, 1],
    ['ArrowLeft', false, -1],
    ['ArrowDown', false, -1],
    ['ArrowRight', true, 10],
    ['ArrowUp', true, 10],
    ['ArrowLeft', true, -10],
    ['ArrowDown', true, -10],
    ['PageUp', false, 10],
    ['PageDown', false, -10],
    ['Home', false, 'min'],
    ['End', false, 'max'],
    ['Enter', false, null],
    ['a', false, null],
    ['Tab', true, null],
  ] as const)('%s (shift %s) -> %s', (key, shift, out) => {
    expect(keyStep(key, shift)).toBe(out)
  })
})

describe('clampThreshold', () => {
  it('rounds and keeps [0, max(500, base)]', () => {
    expect(clampThreshold(-3, 60)).toBe(0)
    expect(clampThreshold(501, 60)).toBe(500)
    expect(clampThreshold(640, 620)).toBe(620)
    expect(clampThreshold(610, 620)).toBe(610)
    expect(clampThreshold(64.6, 60)).toBe(65)
  })
})

describe('single-sensor draft (absolute, G32)', () => {
  it('setThreshold keeps only cells that differ from the device', () => {
    let d = setThreshold(draft(), 0, 'trigger', 65)
    expect(d.edit.trigger).toEqual([65, N, N, N, N, N, N])
    d = setThreshold(d, 0, 'trigger', 60)
    expect(d.edit.trigger).toEqual([N, N, N, N, N, N, N])
    expect(toSensorEdit(d.edit).zone_thresholds).toBeNull()
  })

  it('toSensorEdit sends every key and thresholds as absolute', () => {
    const d = setSection(setThreshold(draft(), 2, 'maintain', 18), 'sensitivity', 3)
    expect(toDraftIn(d)).toEqual({
      targets: [deviceId(1)],
      changes: {
        sensitivity: 3,
        zone_enable: null,
        zone_thresholds: { mode: 'absolute', trigger: [N, N, N, N, N, N, N], maintain: [N, N, 18, N, N, N, N] },
        subsensor_zones: null,
        subsensor_timing: null,
        subsensor_enable: null,
        dnd: null,
      },
      expect_rev: null,
    })
    expect(changedCount(d)).toBe(2)
  })

  it('setSection: equal to the device -> null; sub-sensor zones compare sorted', () => {
    let d = setSection(draft(), 'subsensor_zones', [[2, 0, 1], [3, 4], [5, 6]])
    expect(d.edit.subsensor_zones).toBeNull()
    d = setSection(d, 'subsensor_zones', [[1, 0], [3, 4], [5, 6]])
    expect(d.edit.subsensor_zones).toEqual([[0, 1], [3, 4], [5, 6]])
    expect(changedCount(d)).toBe(1)
    d = setSection(d, 'subsensor_timing', [[10, 60], [5, 30], [5, 30]])
    expect(changedCount(d)).toBe(3) // presence and absence are two rows
  })

  it('rebaseDraft drops edits the new base already has', () => {
    let d = setThreshold(draft(), 0, 'trigger', 65)
    d = setThreshold(d, 1, 'trigger', 70)
    const moved = configView(1, { config_rev: 1, profile: { ...profile(), zone_thresholds: profile().zone_thresholds.map((z, i) => (i === 0 ? { ...z, trigger: 65 } : z)) } })
    const r = rebaseDraft(d, moved)
    expect(r.base.config_rev).toBe(1)
    expect(r.edit.trigger).toEqual([N, 70, N, N, N, N, N])
  })

  it('rebaseDraft carries only the touched elements: another change underneath is kept, not reverted', () => {
    let d = setSection(draft(), 'zone_enable', [true, true, false, true, true, true, false]) // Z2 off here
    d = setSection(d, 'subsensor_timing', [[9, 30], [5, 30], [5, 30]]) // sub-sensor 0 presence
    d = setSection(d, 'subsensor_zones', [[0, 1], [3, 4], [5, 6]])
    d = setSection(d, 'subsensor_enable', [false, true, true])
    // meanwhile another client turned Z5 off, changed sub-sensor 0's absence and 1's zones, disabled sub-sensor 2
    const moved = configView(1, {
      config_rev: 1,
      profile: profile({
        zone_enable: [true, true, true, true, true, false, false],
        subsensor_timing: [[5, 45], [5, 30], [5, 30]],
        subsensor_zones: [[0, 1, 2], [3], [5, 6]],
        subsensor_enable: [true, true, false],
      }),
    })
    const r = rebaseDraft(d, moved)
    expect(r.edit.zone_enable).toEqual([true, true, false, true, true, false, false])
    expect(r.edit.subsensor_timing).toEqual([[9, 45], [5, 30], [5, 30]])
    expect(r.edit.subsensor_zones).toEqual([[0, 1], [3], [5, 6]])
    expect(r.edit.subsensor_enable).toEqual([false, true, false])
    expect(toSensorEdit(r.edit).zone_enable?.[5]).toBe(false) // the apply would not switch Z5 back on
    // an edit the new base already has drops out
    const same = configView(1, { profile: profile({ zone_enable: [true, true, false, true, true, true, false] }) })
    expect(rebaseDraft(d, same).edit.zone_enable).toBeNull()
  })
})

describe('bulk draft (relative by default, D8)', () => {
  const ids = [1, 2, 3].map(deviceId)

  it('nothing to change -> null', () => {
    expect(bulkToDraftIn(emptyBulk(ids))).toBeNull()
  })

  it('relative 0 is "unchanged"', () => {
    const b: BulkDraft = { ...emptyBulk(ids), trigger: [5, 0, 5, 5, 5, 5, 5] }
    expect(bulkToDraftIn(b)?.changes.zone_thresholds).toEqual({
      mode: 'relative',
      trigger: [5, N, 5, 5, 5, 5, 5],
      maintain: [N, N, N, N, N, N, N],
    })
    expect(bulkToDraftIn({ ...emptyBulk(ids), trigger: [0, 0, 0, 0, 0, 0, 0] })).toBeNull()
  })

  it('absolute keeps 0 and sends all 7 zone flags', () => {
    const b: BulkDraft = {
      ...setBulkMode(emptyBulk(ids), 'absolute'),
      maintain: [0, N, N, N, N, N, N],
      zone_enable: [true, true, true, true, true, false, false],
    }
    const req = bulkToDraftIn(b)
    expect(req?.targets).toEqual(ids)
    expect(req?.changes.zone_thresholds?.mode).toBe('absolute')
    expect(req?.changes.zone_thresholds?.maintain[0]).toBe(0)
    expect(req?.changes.zone_enable).toHaveLength(7)
  })

  it('setBulkMode clears the values and the acknowledgement (+5 and 5 differ)', () => {
    const b: BulkDraft = { ...emptyBulk(ids), mode: 'absolute', trigger: [5, N, N, N, N, N, N], absoluteAck: true, sensitivity: 3 }
    const r = setBulkMode(b, 'relative')
    expect(r.trigger).toEqual([N, N, N, N, N, N, N])
    expect(r.absoluteAck).toBe(false)
    expect(r.sensitivity).toBe(3)
    expect(r.ids).toEqual(ids)
  })
})

describe('expectRevOf and scopeOf', () => {
  it('expect_rev is every item config_rev', () => {
    const p = previewRisky()
    p.items[1] = { ...p.items[1]!, config_rev: 4 }
    expect(expectRevOf(p)).toEqual({ [deviceId(1)]: 0, [deviceId(2)]: 4, [deviceId(3)]: 0 })
  })

  it.each([
    ['/sensors/a/settings', 'sensor:a'],
    ['/sensors/a', 'sensor:a'],
    ['/sensors/a/history?x=1', 'sensor:a'],
    ['/bulk', 'bulk'],
    ['/bulk?mode=clone', 'bulk'],
    ['/', null],
    ['/monitor', null],
  ])('%s -> %s', (path, scope) => {
    expect(scopeOf(path)).toBe(scope)
  })
})
