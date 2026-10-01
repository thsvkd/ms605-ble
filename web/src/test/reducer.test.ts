import { describe, expect, it } from 'vitest'
import type { ServerMessage } from '../api/types'
import { addWatch, type AppState, initialState, NOTICE_LIMIT, reduce, removeWatch } from '../store/reducer'
import {
  BATCH_ID,
  batchView,
  deviceId,
  job,
  live,
  liveData,
  NOW_S,
  pending,
  SITE_A,
  SITE_B,
  sensor,
  snapshotMsg,
} from './fixtures'

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
    s = reduce(
      s,
      msg({ type: 'gather', seq: 13, data: { gathering: true, connecting: [{ address: 'a', since: 1 }] } }),
    ).state
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
    const { state, resync } = reduce(
      s0,
      msg({ type: 'sensor_removed', seq: 10, data: { device_id: sensor(1).device_id } }),
    )
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

describe('reduce: M3 batch, countdown and live (14.8.2)', () => {
  const RECEIVED = 5_000_000
  const waiting = batchView({ state: 'waiting', start: 'delay', fire_at: NOW_S + 42 })

  it('a snapshot sets the batch and its countdown from fire_at - ts, and empties live', () => {
    const before: AppState = { ...initialState, live: { x: liveData(1) }, watch: { x: 2 } }
    const { state } = reduce(before, snapshotMsg({ seq: 10, batch: waiting }), RECEIVED)
    expect(state.batch).toBe(waiting)
    expect(state.countdown).toEqual({ batch_id: BATCH_ID, remaining_s: 42, received_at: RECEIVED })
    expect(state.live).toEqual({})
    expect(state.watch).toEqual({ x: 2 }) // the screens still watch: ws.ts re-subscribes
    expect(reduce(initialState, snapshotMsg({ batch: batchView() })).state.countdown).toBeNull()
  })

  it('batch replaces the batch; waiting re-anchors the countdown, anything else clears it', () => {
    let s = fromSnapshot()
    s = reduce(s, msg({ type: 'batch', seq: 11, data: waiting }), RECEIVED).state
    expect(s.countdown).toEqual({ batch_id: BATCH_ID, remaining_s: 42, received_at: RECEIVED })
    const running = batchView({ state: 'running' })
    s = reduce(s, msg({ type: 'batch', seq: 12, data: running }), RECEIVED).state
    expect(s.batch).toBe(running)
    expect(s.countdown).toBeNull()
    expect(s.lastSeq).toBe(12)
  })

  it('calibration_job swaps that job in place for the same batch only', () => {
    let s = reduce(fromSnapshot(), msg({ type: 'batch', seq: 11, data: batchView({ state: 'running' }) })).state
    const learning = job(2, { state: 'learning', elapsed_s: 12 })
    s = reduce(s, msg({ type: 'calibration_job', seq: 12, data: learning })).state
    expect(s.batch?.jobs.map((j) => j.device_id)).toEqual([1, 2, 3].map(deviceId))
    expect(s.batch?.jobs[1]).toBe(learning)
    const other = job(1, { batch_id: 'b-9999', state: 'failed' })
    const r = reduce(s, msg({ type: 'calibration_job', seq: 13, data: other }))
    expect(r.state.batch).toBe(s.batch)
    expect(r.state.lastSeq).toBe(13)
  })

  it('live is kept only for watched sensors and never touches lastSeq', () => {
    let s = addWatch(fromSnapshot(), [deviceId(1)])
    s = reduce(s, { type: 'live', seq: null, ts: NOW_S, data: liveData(1) }).state
    expect(s.live[deviceId(1)]?.zones).toHaveLength(7)
    const unwatched = reduce(s, { type: 'live', seq: null, ts: NOW_S, data: liveData(2) })
    expect(unwatched.state).toBe(s)
    expect(s.lastSeq).toBe(10)
  })

  it('countdown applies only to the same, still waiting batch', () => {
    const s = reduce(fromSnapshot(), msg({ type: 'batch', seq: 11, data: waiting }), 1).state
    const c = (batch_id: string, remaining_s: number) =>
      ({ type: 'countdown', seq: null, ts: NOW_S, data: { batch_id, fire_at: NOW_S + 42, remaining_s } }) as const
    expect(reduce(s, c(BATCH_ID, 30), RECEIVED).state.countdown).toEqual({
      batch_id: BATCH_ID,
      remaining_s: 30,
      received_at: RECEIVED,
    })
    expect(reduce(s, c('b-9999', 30)).state).toBe(s)
    const running = reduce(s, msg({ type: 'batch', seq: 12, data: batchView({ state: 'running' }) })).state
    expect(reduce(running, c(BATCH_ID, 1)).state.countdown).toBeNull() // a late tick after RUNNING
  })

  it('removeWatch at zero drops the frame; two adds and one remove keep it', () => {
    const id = deviceId(1)
    let s = addWatch(addWatch(fromSnapshot(), [id]), [id])
    s = reduce(s, { type: 'live', seq: null, ts: NOW_S, data: liveData(1) }).state
    s = removeWatch(s, [id])
    expect(s.watch[id]).toBe(1)
    expect(s.live[id]).toBeDefined()
    s = removeWatch(s, [id])
    expect(id in s.watch).toBe(false)
    expect(s.live[id]).toBeUndefined()
  })
})
