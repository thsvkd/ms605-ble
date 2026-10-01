import { describe, expect, it } from 'vitest'
import type { ServerMessage } from '../api/types'
import { type AppState, initialState, NOTICE_LIMIT, reduce } from '../store/reducer'
import { live, NOW_S, SITE_A, SITE_B, sensor, snapshotMsg, pending } from './fixtures'

const fromSnapshot = (): AppState =>
  reduce(initialState, snapshotMsg({ seq: 10, sensors: [sensor(1, { live: live(1) }), sensor(2)] })).state

const msg = (m: Omit<ServerMessage, 'ts'>): ServerMessage => ({ ts: NOW_S, ...m }) as ServerMessage

describe('reduce', () => {
  it('a snapshot replaces the whole state and sets lastSeq', () => {
    const before: AppState = {
      ...initialState,
      lastSeq: 99,
      sensors: { stale: sensor(9) },
      sites: { old: { site_id: 'old', name: 'Old' } },
    }
    const { state, resync } = reduce(before, snapshotMsg({ seq: 3, sites: [SITE_A], sensors: [sensor(1)] }))
    expect(resync).toBe(false)
    expect(state.lastSeq).toBe(3)
    expect(Object.keys(state.sensors)).toEqual([sensor(1).device_id])
    expect(Object.keys(state.sites)).toEqual(['lab-a'])
    expect(state.server?.sim?.count).toBe(3)
  })

  it('upserts a sensor', () => {
    const s0 = fromSnapshot()
    const updated = sensor(1, { live: live(1, { link: 'lost' }) })
    const { state } = reduce(s0, msg({ type: 'sensor', seq: 11, data: updated }))
    expect(state.sensors[updated.device_id]?.live?.link).toBe('lost')
    const added = sensor(7)
    const { state: s2 } = reduce(state, msg({ type: 'sensor', seq: 12, data: added }))
    expect(Object.keys(s2.sensors)).toHaveLength(3)
    expect(s2.lastSeq).toBe(12)
  })

  it('removes a sensor', () => {
    const s0 = fromSnapshot()
    const { state } = reduce(s0, msg({ type: 'sensor_removed', seq: 11, data: { device_id: sensor(1).device_id } }))
    expect(state.sensors[sensor(1).device_id]).toBeUndefined()
    expect(state.lastSeq).toBe(11)
  })

  it('replaces sites, pending and gather', () => {
    let s = fromSnapshot()
    s = reduce(s, msg({ type: 'sites', seq: 11, data: { sites: [SITE_A, SITE_B] } })).state
    expect(Object.keys(s.sites)).toEqual(['lab-a', 'lab-b'])
    s = reduce(s, msg({ type: 'pending', seq: 12, data: { pending: [pending(SITE_B, 'P', 9)] } })).state
    expect(s.pending).toHaveLength(1)
    s = reduce(s, msg({ type: 'gather', seq: 13, data: { gathering: true, connecting: [{ address: 'a', since: 1 }] } }))
      .state
    expect(s.gather.gathering).toBe(true)
    expect(s.gather.connecting).toHaveLength(1)
    expect(s.lastSeq).toBe(13)
  })

  it(`keeps only the latest ${NOTICE_LIMIT} notices`, () => {
    let s = fromSnapshot()
    for (let i = 0; i < NOTICE_LIMIT + 5; i++) {
      const data = {
        level: 'warning' as const,
        code: 'gather_failed' as const,
        message: `m${i}`,
        device_id: null,
        address: null,
        name: null,
        at: NOW_S + i,
      }
      s = reduce(s, msg({ type: 'notice', seq: 11 + i, data })).state
    }
    expect(s.notices).toHaveLength(NOTICE_LIMIT)
    expect(s.notices.at(-1)?.message).toBe(`m${NOTICE_LIMIT + 4}`)
    expect(s.notices[0]?.message).toBe('m5')
  })

  it('ignores a duplicate seq', () => {
    const s0 = fromSnapshot()
    const { state, resync } = reduce(s0, msg({ type: 'sensor_removed', seq: 10, data: { device_id: sensor(1).device_id } }))
    expect(resync).toBe(false)
    expect(state).toBe(s0)
  })

  it('asks for a resync on a gap and leaves the state alone', () => {
    const s0 = fromSnapshot()
    const { state, resync } = reduce(s0, msg({ type: 'sensor', seq: 12, data: sensor(8) }))
    expect(resync).toBe(true)
    expect(state).toBe(s0)
  })

  it('asks for a resync when a delta arrives before any snapshot', () => {
    expect(reduce(initialState, msg({ type: 'sensor', seq: 1, data: sensor(1) })).resync).toBe(true)
  })

  it('advances lastSeq on an unknown message type without touching data', () => {
    const s0 = fromSnapshot()
    const { state, resync } = reduce(s0, { type: 'live_radar_v9', seq: 11, data: {} })
    expect(resync).toBe(false)
    expect(state.lastSeq).toBe(11)
    expect(state.sensors).toBe(s0.sensors)
  })

  it('keeps lastSeq a number when a message has no seq at all', () => {
    const s0 = fromSnapshot()
    const malformed = { type: 'live_radar', data: {} } as unknown as Parameters<typeof reduce>[1]
    const { state, resync } = reduce(s0, malformed)
    expect(resync).toBe(false)
    expect(state.lastSeq).toBe(10)
    expect(reduce(state, { type: 'live_radar_v9', seq: 12, data: {} }).resync).toBe(true) // gap still caught
  })

  it('applies seq: null messages without ordering checks', () => {
    const s0 = fromSnapshot()
    const { state, resync } = reduce(s0, { type: 'live_radar', seq: null, data: {} })
    expect(resync).toBe(false)
    expect(state.lastSeq).toBe(10)
  })
})
