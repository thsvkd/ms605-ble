import { describe, expect, it } from 'vitest'
import { NO_PRESENCE, presenceFlips, presenceOf, presenceTally, signalTone } from '../presence'
import { deviceId, liveData, liveZone } from './fixtures'

const quiet = (n: number) =>
  liveData(n, {
    pir: false,
    sub_sensor_presence: [false, false, false],
    zones: [0, 1, 2, 3, 4, 5, 6].map((i) => liveZone(i)),
  })

describe('presenceOf', () => {
  it('reads PIR as sent, RF from enabled trigger flags, 재실 from the device sub-sensor calls', () => {
    expect(presenceOf(liveData(1))).toEqual({ pir: true, rf: true, present: true, subs: [true, false, false] })
    expect(presenceOf(quiet(1))).toEqual({ pir: false, rf: false, present: false, subs: [false, false, false] })
  })

  it('without a frame everything is unknown', () => {
    expect(presenceOf(undefined)).toBe(NO_PRESENCE)
    expect(NO_PRESENCE).toEqual({ pir: null, rf: null, present: null, subs: null })
  })

  it('keeps PIR null as unknown', () => {
    expect(presenceOf(liveData(1, { pir: null })).pir).toBeNull()
  })

  it('ignores the trigger flag of a disabled zone', () => {
    const f = quiet(1)
    f.zones[6] = liveZone(6, { enabled: false, trigger_active: true, trigger: 99 })
    expect(presenceOf(f).rf).toBe(false)
    f.zones[3] = liveZone(3, { trigger_active: true })
    expect(presenceOf(f).rf).toBe(true)
  })

  it('uses the device flag, not the host comparison, for RF', () => {
    const f = quiet(1)
    f.zones[0] = liveZone(0, { trigger: 80, trigger_threshold: 55, trigger_active: false })
    expect(presenceOf(f).rf).toBe(false)
  })

  it('never derives 재실 from PIR or RF on the host', () => {
    // PIR and RF both fire, the device has not called presence: 부재
    const f = liveData(1, { pir: true, sub_sensor_presence: [false, false, false] })
    expect(presenceOf(f)).toMatchObject({ pir: true, rf: true, present: false })
    // the device holds presence with neither raw signal firing: 재실
    const g = quiet(1)
    g.sub_sensor_presence = [false, true, false]
    expect(presenceOf(g)).toMatchObject({ pir: false, rf: false, present: true })
  })
})

describe('signalTone', () => {
  it('raw signals that fire are hits, the 재실 call is present, null is unknown', () => {
    expect(signalTone('pir', true)).toBe('hit')
    expect(signalTone('rf', true)).toBe('hit')
    expect(signalTone('presence', true)).toBe('present')
    expect(signalTone('pir', false)).toBe('off')
    expect(signalTone('presence', false)).toBe('off')
    expect(signalTone('rf', null)).toBe('unknown')
    expect(signalTone('presence', null)).toBe('unknown')
  })
})

describe('presenceTally', () => {
  const on = (frame: Parameters<typeof presenceOf>[0]) => ({ frame, connected: true })

  it('counts each signal over the connected sensors, and the connected ones without a frame', () => {
    const pirOnly = quiet(2)
    pirOnly.pir = true
    expect(presenceTally([on(liveData(1)), on(pirOnly), on(quiet(3)), on(undefined)])).toEqual({
      total: 4,
      present: 1,
      pir: 2,
      rf: 1,
      unknown: 1,
      offline: 0,
    })
    expect(presenceTally([])).toEqual({ total: 0, present: 0, pir: 0, rf: 0, unknown: 0, offline: 0 })
  })

  it('never counts the last frame of a sensor that is not connected now as a detection', () => {
    // a lost sensor whose last frame said 재실, PIR and RF: history, not "now"
    expect(presenceTally([on(quiet(1)), { frame: liveData(2), connected: false }])).toEqual({
      total: 2,
      present: 0,
      pir: 0,
      rf: 0,
      unknown: 0,
      offline: 1,
    })
  })
})

describe('presenceFlips', () => {
  const a = deviceId(1)
  const b = deviceId(2)

  it('reports a flip either way', () => {
    expect(presenceFlips({ [a]: quiet(1) }, { [a]: liveData(1) })).toEqual([{ id: a, present: true }])
    expect(presenceFlips({ [a]: liveData(1) }, { [a]: quiet(1) })).toEqual([{ id: a, present: false }])
  })

  it('stays quiet on the same call, a first frame, a frame leaving, or the same map', () => {
    const next = { [a]: liveData(1, { at: 2 }), [b]: liveData(2) }
    expect(presenceFlips({ [a]: liveData(1, { at: 1 }) }, next)).toEqual([])
    expect(presenceFlips({ [a]: liveData(1) }, {})).toEqual([])
    expect(presenceFlips(next, next)).toEqual([])
  })
})
