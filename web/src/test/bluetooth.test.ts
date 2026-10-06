import { afterEach, describe, expect, it, vi } from 'vitest'
import { waitFor } from '@testing-library/react'
import { startGather } from '../api/client'
import { resetStore, useStore } from '../store/store'
import { mockApi } from './api'
import { storeState } from './fixtures'

class BrowserSocket {
  static sockets: BrowserSocket[] = []
  readyState = 0
  onopen: (() => void) | null = null
  onmessage: ((event: MessageEvent) => void) | null = null
  onclose: ((event: CloseEvent) => void) | null = null
  onerror: (() => void) | null = null
  sent: Record<string, unknown>[] = []

  readonly url: string
  constructor(url: string) {
    this.url = url
    BrowserSocket.sockets.push(this)
    queueMicrotask(() => { this.readyState = 1; this.onopen?.() })
  }
  send(data: string) {
    const message = JSON.parse(data)
    this.sent.push(message)
    if (message.type === 'hello') queueMicrotask(() => this.receive({ type: 'ready' }))
  }
  receive(data: unknown) { this.onmessage?.(new MessageEvent('message', { data: JSON.stringify(data) })) }
  close(code = 1000) { this.readyState = 3; this.onclose?.(new CloseEvent('close', { code })) }
}

function browser() {
  resetStore(storeState({ server: { ble_transport: 'browser', version: 'test', lan: true, sim: null } }))
  vi.stubGlobal('isSecureContext', true)
  vi.stubGlobal('WebSocket', BrowserSocket)
  const notify = Object.assign(new EventTarget(), {
    value: undefined as DataView | undefined,
    startNotifications: vi.fn(async () => notify),
  })
  const write = { writeValueWithoutResponse: vi.fn(async (_data: Uint8Array) => {}) }
  const service = { getCharacteristic: vi.fn(async (uuid: string) => uuid.includes('-0002-') ? write : notify) }
  const gatt = {
    connected: false,
    connect: vi.fn(async () => { gatt.connected = true; return gatt }),
    disconnect: vi.fn(() => { gatt.connected = false }),
    getPrimaryService: vi.fn(async () => service),
  }
  const device = Object.assign(new EventTarget(), { id: 'synthetic-device', name: 'RFBL_ABCDEF', gatt })
  const requestDevice = vi.fn(async () => device)
  Object.defineProperty(navigator, 'bluetooth', { configurable: true, value: { requestDevice } })
  const calls = mockApi({ 'POST /api/gather/start': { status: 200, body: { gathering: true, connecting: [] } } })
  return { device, gatt, notify, write, requestDevice, calls }
}

afterEach(() => {
  for (const socket of BrowserSocket.sockets) socket.close()
  BrowserSocket.sockets = []
  Reflect.deleteProperty(navigator, 'bluetooth')
})

describe('client Bluetooth transport', () => {
  it('offers a sensor advertising only the MS605 manufacturer signature, without a service UUID', async () => {
    const { requestDevice } = browser()
    await startGather()
    // Synthetic advertisement: match the radio shape seen on MS605, without device identifiers.
    const advertisement = { name: 'ms605', services: [] as string[], company: 0xffff, data: [0xc0, 0x00] }
    type Filter = {
      services?: string[]
      namePrefix?: string
      manufacturerData?: { companyIdentifier: number; dataPrefix: Uint8Array }[]
    }
    const options = requestDevice.mock.calls[0] as unknown as [{ filters: Filter[] }]
    const visible = options[0].filters.some((filter) =>
      (filter.services?.every((uuid) => advertisement.services.includes(uuid)) ?? false) ||
      (filter.namePrefix !== undefined && advertisement.name.startsWith(filter.namePrefix)) ||
      (filter.manufacturerData?.some((data) => data.companyIdentifier === advertisement.company &&
        Array.from(data.dataPrefix).every((byte, index) => advertisement.data[index] === byte)) ?? false),
    )
    expect(visible).toBe(true)
  })

  it('refuses an unsupported browser without starting a server scan', async () => {
    const { calls } = browser()
    Reflect.deleteProperty(navigator, 'bluetooth')
    await expect(startGather()).rejects.toThrow(/Chrome|Edge/)
    expect(calls).toHaveLength(0)
  })

  it('requires HTTPS on remote clients before requesting Bluetooth', async () => {
    const { requestDevice, calls } = browser()
    vi.stubGlobal('isSecureContext', false)
    await expect(startGather()).rejects.toThrow(/HTTPS/)
    expect(requestDevice).not.toHaveBeenCalled()
    expect(calls).toHaveLength(0)
  })

  it('opens the client chooser synchronously from the user action, then registers the bridge', async () => {
    const { requestDevice, calls } = browser()
    const pending = startGather()
    expect(requestDevice).toHaveBeenCalledWith({
      filters: [
        { services: ['99e7be30-0001-4c6b-98a2-70fcb3471a72'] },
        { namePrefix: 'RFBL_' },
        { namePrefix: 'MRBL_' },
        { manufacturerData: [{ companyIdentifier: 0xffff, dataPrefix: new Uint8Array([0xc0]) }] },
      ],
      optionalServices: ['99e7be30-0001-4c6b-98a2-70fcb3471a72'],
      optionalManufacturerData: [0xffff],
    })
    await expect(pending).resolves.toEqual({ gathering: true, connecting: [] })
    expect(BrowserSocket.sockets[0]?.url).toMatch(/\/ws\/ble$/)
    expect(BrowserSocket.sockets[0]?.sent[0]).toEqual({ type: 'hello', name: 'RFBL_ABCDEF' })
    expect(calls.map((call) => call.path)).toEqual(['/api/gather/start'])
  })

  it('relays manufacturer advertisement bytes in hello and stops watching', async () => {
    const { device } = browser()
    let signal: AbortSignal | undefined
    const remove = vi.spyOn(device, 'removeEventListener')
    Object.assign(device, {
      watchAdvertisements: vi.fn(async (options?: { signal?: AbortSignal }) => {
        signal = options?.signal
        queueMicrotask(() => device.dispatchEvent(Object.assign(new Event('advertisementreceived'), {
          manufacturerData: new Map([[0xffff, new DataView(new Uint8Array([0xc0, 0x18, 0x01, 0x00]).buffer)]]),
        })))
      }),
    })

    await startGather()

    expect(BrowserSocket.sockets[0]?.sent[0]).toEqual({
      type: 'hello',
      name: 'RFBL_ABCDEF',
      manufacturer_data: [0xc0, 0x18, 0x01, 0x00],
    })
    expect(signal?.aborted).toBe(true)
    expect(remove).toHaveBeenCalledWith('advertisementreceived', expect.any(Function))
  })

  it('continues pairing and cleans up when advertisement watching is rejected', async () => {
    const { device } = browser()
    let signal: AbortSignal | undefined
    const remove = vi.spyOn(device, 'removeEventListener')
    Object.assign(device, {
      watchAdvertisements: vi.fn(async (options?: { signal?: AbortSignal }) => {
        signal = options?.signal
        throw new DOMException('unsupported', 'NotSupportedError')
      }),
    })

    await startGather()

    expect(BrowserSocket.sockets[0]?.sent[0]).toEqual({ type: 'hello', name: 'RFBL_ABCDEF' })
    expect(signal?.aborted).toBe(true)
    expect(remove).toHaveBeenCalledWith('advertisementreceived', expect.any(Function))
  })

  it('continues pairing after the advertisement timeout and stops watching', async () => {
    const { device } = browser()
    let signal: AbortSignal | undefined
    const remove = vi.spyOn(device, 'removeEventListener')
    Object.assign(device, {
      watchAdvertisements: vi.fn(async (options?: { signal?: AbortSignal }) => { signal = options?.signal }),
    })
    vi.useFakeTimers()
    try {
      const pending = startGather()
      await vi.advanceTimersByTimeAsync(3_000)
      await pending
      expect(BrowserSocket.sockets[0]?.sent[0]).toEqual({ type: 'hello', name: 'RFBL_ABCDEF' })
      expect(signal?.aborted).toBe(true)
      expect(remove).toHaveBeenCalledWith('advertisementreceived', expect.any(Function))
    } finally {
      vi.useRealTimers()
    }
  })

  it('cancelling the chooser does not start gathering', async () => {
    const { requestDevice, calls } = browser()
    requestDevice.mockRejectedValue(new DOMException('cancelled', 'NotFoundError'))
    await expect(startGather()).resolves.toEqual({ gathering: false, connecting: [] })
    expect(calls).toHaveLength(0)
    expect(BrowserSocket.sockets).toHaveLength(0)
  })

  it('explains client Bluetooth permissions instead of a Linux adapter hint', async () => {
    const { requestDevice, calls } = browser()
    requestDevice.mockRejectedValue(new DOMException('denied', 'SecurityError'))
    await expect(startGather()).rejects.toThrow(/Bluetooth.*권한/)
    expect(calls).toHaveLength(0)
  })

  it('relays GATT writes and exact notification bytes, and reports link loss', async () => {
    const { device, gatt, notify, write } = browser()
    await startGather()
    const socket = BrowserSocket.sockets[0]!
    socket.receive({ type: 'request', id: 1, op: 'connect' })
    await waitFor(() => expect(socket.sent).toContainEqual({ type: 'result', id: 1 }))
    expect(gatt.getPrimaryService).toHaveBeenCalledWith('99e7be30-0001-4c6b-98a2-70fcb3471a72')
    socket.receive({ type: 'request', id: 2, op: 'start_notify' })
    await waitFor(() => expect(socket.sent).toContainEqual({ type: 'result', id: 2 }))
    socket.receive({ type: 'request', id: 3, op: 'write', data: [0x55, 0xaa, 0xc0] })
    await waitFor(() => expect(socket.sent).toContainEqual({ type: 'result', id: 3 }))
    expect(write.writeValueWithoutResponse).toHaveBeenCalledWith(new Uint8Array([0x55, 0xaa, 0xc0]))
    notify.value = new DataView(new Uint8Array([99, 0x55, 0xaa, 88]).buffer, 1, 2)
    notify.dispatchEvent(new Event('characteristicvaluechanged'))
    expect(socket.sent).toContainEqual({ type: 'notify', data: [0x55, 0xaa] })
    device.dispatchEvent(new Event('gattserverdisconnected'))
    expect(socket.sent).toContainEqual({ type: 'disconnected' })
    socket.close()
    expect(gatt.disconnect).toHaveBeenCalled()
  })

  it('closes the client link when the start request is rejected', async () => {
    const { gatt } = browser()
    mockApi({ 'POST /api/gather/start': { status: 401, body: { error: { code: 'unauthorized', message: 'denied' } } } })
    await expect(startGather()).rejects.toMatchObject({ status: 401 })
    expect(BrowserSocket.sockets[0]?.readyState).toBe(3)
    expect(gatt.disconnect).toHaveBeenCalled()
    expect(useStore.getState().conn).toBe('unauthorized')
  })

  it('acknowledges intentional disconnect without reporting a lost pending command', async () => {
    const { device, gatt } = browser()
    await startGather()
    const socket = BrowserSocket.sockets[0]!
    gatt.disconnect.mockImplementation(() => {
      gatt.connected = false
      device.dispatchEvent(new Event('gattserverdisconnected'))
    })
    socket.receive({ type: 'request', id: 1, op: 'disconnect' })
    await waitFor(() => expect(socket.sent).toContainEqual({ type: 'result', id: 1 }))
    expect(socket.sent).not.toContainEqual({ type: 'disconnected' })
  })

  it('reconnects and resubscribes through the same bridge after a GATT drop', async () => {
    const { device, gatt, notify } = browser()
    await startGather()
    const socket = BrowserSocket.sockets[0]!
    for (const [id, op] of [[1, 'connect'], [2, 'start_notify']] as const) {
      socket.receive({ type: 'request', id, op })
      await waitFor(() => expect(socket.sent).toContainEqual({ type: 'result', id }))
    }
    gatt.connected = false
    device.dispatchEvent(new Event('gattserverdisconnected'))
    for (const [id, op] of [[3, 'connect'], [4, 'start_notify'], [5, 'start_notify']] as const) {
      socket.receive({ type: 'request', id, op })
      await waitFor(() => expect(socket.sent).toContainEqual({ type: 'result', id }))
    }
    notify.value = new DataView(new Uint8Array([0x55, 0xaa]).buffer)
    notify.dispatchEvent(new Event('characteristicvaluechanged'))
    expect(socket.sent.filter((message) => message.type === 'notify')).toEqual([{ type: 'notify', data: [0x55, 0xaa] }])
    expect(gatt.connect).toHaveBeenCalledTimes(2)
  })

  it('disconnects a GATT connection that finishes after the owning socket closes', async () => {
    const { gatt } = browser()
    let connected!: () => void
    gatt.connect.mockImplementation(() => new Promise((resolve) => {
      connected = () => { gatt.connected = true; resolve(gatt) }
    }))
    await startGather()
    const socket = BrowserSocket.sockets[0]!
    socket.receive({ type: 'request', id: 1, op: 'connect' })
    await waitFor(() => expect(gatt.connect).toHaveBeenCalled())
    socket.close()
    connected()
    await waitFor(() => expect(gatt.connected).toBe(false))
  })

  it('cleans up a stalled GATT operation so the sensor can be selected again', async () => {
    const { gatt } = browser()
    await startGather()
    vi.useFakeTimers()
    try {
      gatt.connect.mockImplementation(() => new Promise(() => {}))
      const socket = BrowserSocket.sockets[0]!
      socket.receive({ type: 'request', id: 1, op: 'connect' })
      await vi.advanceTimersByTimeAsync(15_000)
      expect(socket.readyState).toBe(3)
      expect(gatt.disconnect).toHaveBeenCalled()
      const pending = startGather()
      await vi.advanceTimersByTimeAsync(0)
      await pending
      expect(BrowserSocket.sockets).toHaveLength(2)
    } finally {
      vi.useRealTimers()
    }
  })

  it('keeps a new connection when a timed-out older connect completes late', async () => {
    const { gatt } = browser()
    let oldConnected!: () => void
    gatt.connect.mockImplementationOnce(() => new Promise((resolve) => {
      oldConnected = () => { gatt.connected = true; resolve(gatt) }
    }))
    await startGather()
    vi.useFakeTimers()
    try {
      const oldSocket = BrowserSocket.sockets[0]!
      oldSocket.receive({ type: 'request', id: 1, op: 'connect' })
      await vi.advanceTimersByTimeAsync(10_000)
      expect(oldSocket.readyState).toBe(3)
      const pending = startGather()
      await vi.advanceTimersByTimeAsync(0)
      await pending
      const newSocket = BrowserSocket.sockets[1]!
      newSocket.receive({ type: 'request', id: 1, op: 'connect' })
      await vi.advanceTimersByTimeAsync(0)
      expect(newSocket.sent).toContainEqual({ type: 'result', id: 1 })
      expect(gatt.connected).toBe(true)
      oldConnected()
      await vi.advanceTimersByTimeAsync(0)
      expect(gatt.connected).toBe(true)
      expect(newSocket.readyState).toBe(1)
      expect(newSocket.sent).not.toContainEqual({ type: 'disconnected' })
    } finally {
      vi.useRealTimers()
    }
  })

  it('keeps the simulator independent of browser Bluetooth', async () => {
    const { requestDevice, calls } = browser()
    resetStore(storeState())
    await startGather()
    expect(requestDevice).not.toHaveBeenCalled()
    expect(calls.map((call) => call.path)).toEqual(['/api/gather/start'])
  })
})
