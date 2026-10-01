import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { backoffMs, connectWs, type SocketLike, type WsHandle } from '../api/ws'
import { useStore } from '../store/store'
import { deviceId, NOW_S, sensor, snapshotMsg } from './fixtures'

class FakeSocket implements SocketLike {
  readyState = 0
  onopen: ((ev: Event) => void) | null = null
  onmessage: ((ev: MessageEvent) => void) | null = null
  onclose: ((ev: CloseEvent) => void) | null = null
  onerror: ((ev: Event) => void) | null = null
  closedWith: number | undefined | null = null
  /** Frames the client sent (live_subscribe / live_unsubscribe). */
  readonly outbox: { type: string; data: { device_ids: string[] } }[] = []
  readonly url: string

  constructor(url: string) {
    this.url = url
  }

  close(code?: number) {
    this.closedWith = code
    this.readyState = 3
  }

  send(data: string) {
    this.outbox.push(JSON.parse(data))
  }

  // -- server side
  open() {
    this.readyState = 1
    this.onopen?.(new Event('open'))
  }

  deliver(msg: unknown) {
    this.onmessage?.(new MessageEvent('message', { data: JSON.stringify(msg) }))
  }

  serverClose(code: number) {
    this.readyState = 3
    this.onclose?.(new CloseEvent('close', { code }))
  }
}

let sockets: FakeSocket[]
let handle: WsHandle | null

function start() {
  handle = connectWs({
    url: 'ws://127.0.0.1:8605/ws',
    random: () => 0.5, // no jitter
    createSocket: (url) => {
      const s = new FakeSocket(url)
      sockets.push(s)
      return s
    },
  })
}

const last = () => sockets[sockets.length - 1] as FakeSocket

function connectWithSnapshot(seq = 10) {
  last().open()
  last().deliver(snapshotMsg({ seq }))
}

beforeEach(() => {
  vi.useFakeTimers()
  sockets = []
  handle = null
})

afterEach(() => {
  handle?.stop()
  vi.useRealTimers()
})

describe('connectWs', () => {
  it('opens on a snapshot', () => {
    start()
    expect(sockets).toHaveLength(1)
    expect(useStore.getState().conn).toBe('connecting')
    connectWithSnapshot()
    expect(useStore.getState()).toMatchObject({ conn: 'open', lastSeq: 10 })
  })

  it.each([4401, 4403])('close %i -> unauthorized and never reconnects', (code) => {
    start()
    last().open()
    last().serverClose(code)
    expect(useStore.getState().conn).toBe('unauthorized')
    vi.advanceTimersByTime(60_000)
    expect(sockets).toHaveLength(1)
  })

  it('close 1013 (too slow) reconnects at once', () => {
    start()
    connectWithSnapshot()
    last().serverClose(1013)
    expect(sockets).toHaveLength(2)
    expect(useStore.getState().conn).toBe('reconnecting')
  })

  it('close 1013 before any snapshot backs off instead of looping', () => {
    start()
    last().open()
    last().serverClose(1013)
    expect(sockets).toHaveLength(1)
    vi.advanceTimersByTime(500)
    expect(sockets).toHaveLength(2)
    last().open()
    last().serverClose(1013)
    vi.advanceTimersByTime(999)
    expect(sockets).toHaveLength(2)
    vi.advanceTimersByTime(1)
    expect(sockets).toHaveLength(3)
  })

  it('a seq gap closes the socket and reconnects for a fresh snapshot', () => {
    start()
    connectWithSnapshot(10)
    const first = last()
    first.deliver({ type: 'sensor', seq: 12, ts: NOW_S, data: sensor(1) })
    expect(first.closedWith).not.toBeNull()
    expect(sockets).toHaveLength(2)
    last().open()
    last().deliver(snapshotMsg({ seq: 40 }))
    expect(useStore.getState()).toMatchObject({ conn: 'open', lastSeq: 40 })
  })

  it('backs off 0.5, 1, 2, 4, 5, 5 s and resets after a snapshot', () => {
    start()
    connectWithSnapshot()
    const expected = [500, 1000, 2000, 4000, 5000, 5000]
    for (const [i, ms] of expected.entries()) {
      last().serverClose(1006)
      expect(sockets).toHaveLength(i + 1)
      vi.advanceTimersByTime(ms - 1)
      expect(sockets).toHaveLength(i + 1)
      vi.advanceTimersByTime(1)
      expect(sockets).toHaveLength(i + 2)
    }
    connectWithSnapshot(50)
    last().serverClose(1001)
    vi.advanceTimersByTime(500)
    expect(sockets).toHaveLength(expected.length + 2)
  })

  it('counts consecutive failures for the banner and clears them on a snapshot', () => {
    start()
    connectWithSnapshot()
    last().serverClose(1006) // the established link drops: not a failed attempt
    expect(useStore.getState()).toMatchObject({ conn: 'reconnecting', failures: 0 })
    vi.advanceTimersByTime(500)
    last().serverClose(1006)
    vi.advanceTimersByTime(1000)
    last().serverClose(1006)
    expect(useStore.getState().failures).toBe(2)
    vi.advanceTimersByTime(2000)
    connectWithSnapshot()
    expect(useStore.getState()).toMatchObject({ conn: 'open', failures: 0 })
  })

  it('reconnects immediately when the page becomes visible again', () => {
    start()
    connectWithSnapshot()
    last().serverClose(1006)
    last() // no new socket yet; the backoff timer is pending
    expect(sockets).toHaveLength(1)
    vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('visible')
    document.dispatchEvent(new Event('visibilitychange'))
    expect(sockets).toHaveLength(2)
    vi.advanceTimersByTime(10_000)
    expect(sockets).toHaveLength(2) // the pending backoff was cancelled
  })

  it('waking while the old socket is still closing keeps exactly one socket', () => {
    start()
    connectWithSnapshot()
    const old = last()
    old.readyState = 2 // CLOSING: its close event has not fired yet
    vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('visible')
    document.dispatchEvent(new Event('visibilitychange'))
    expect(sockets).toHaveLength(2)
    expect(old.closedWith).not.toBeNull() // the replaced socket was closed and detached
    old.serverClose(1006) // its late close event must not schedule another reconnect
    vi.advanceTimersByTime(60_000)
    expect(sockets).toHaveLength(2)
    last().open()
    last().deliver(snapshotMsg({ seq: 20 }))
    expect(useStore.getState()).toMatchObject({ conn: 'open', lastSeq: 20 })
  })

  it('backoffMs applies ±20% jitter', () => {
    expect(backoffMs(0, () => 0)).toBeCloseTo(400)
    expect(backoffMs(0, () => 1)).toBeCloseTo(600)
    expect(backoffMs(99, () => 0.5)).toBe(5000)
  })
})

describe('connectWs: live subscriptions follow `watch` (14.8.3)', () => {
  const ids = (s: FakeSocket, type: string) => s.outbox.filter((m) => m.type === type).flatMap((m) => m.data.device_ids)

  it('sends nothing before the snapshot, then every watched id once', () => {
    useStore.getState().watchLive([deviceId(1), deviceId(2)])
    start()
    last().open()
    useStore.getState().watchLive([deviceId(3)])
    expect(last().outbox).toEqual([])
    last().deliver(snapshotMsg())
    expect(last().outbox).toHaveLength(1)
    expect(ids(last(), 'live_subscribe').sort()).toEqual([1, 2, 3].map(deviceId).sort())
  })

  it('sends only the difference when watch changes', () => {
    start()
    connectWithSnapshot()
    const { watchLive, unwatchLive } = useStore.getState()
    watchLive([deviceId(1)])
    watchLive([deviceId(1)]) // a second view of the same sensor: no new frame
    watchLive([deviceId(2)])
    unwatchLive([deviceId(1)]) // one view left
    expect(last().outbox).toEqual([
      { type: 'live_subscribe', data: { device_ids: [deviceId(1)] } },
      { type: 'live_subscribe', data: { device_ids: [deviceId(2)] } },
    ])
    unwatchLive([deviceId(1)])
    expect(last().outbox.at(-1)).toEqual({ type: 'live_unsubscribe', data: { device_ids: [deviceId(1)] } })
  })

  it('splits 33 ids into 32 + 1', () => {
    start()
    connectWithSnapshot()
    useStore.getState().watchLive(Array.from({ length: 33 }, (_, i) => `id-${i}`))
    expect(last().outbox.map((m) => m.data.device_ids.length)).toEqual([32, 1])
  })

  it('subscribes everything again on the new snapshot after a reconnect', () => {
    start()
    connectWithSnapshot()
    useStore.getState().watchLive([deviceId(1), deviceId(2)])
    last().serverClose(1013)
    expect(sockets).toHaveLength(2)
    last().open()
    expect(last().outbox).toEqual([])
    last().deliver(snapshotMsg({ seq: 30 }))
    expect(ids(last(), 'live_subscribe').sort()).toEqual([deviceId(1), deviceId(2)].sort())
  })

  it('stops mirroring once stopped', () => {
    start()
    connectWithSnapshot()
    const s = last()
    handle?.stop()
    handle = null
    useStore.getState().watchLive([deviceId(1)])
    expect(s.outbox).toEqual([])
  })
})
