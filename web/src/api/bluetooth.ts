import { useStore } from '../store/store'
import { t } from '../strings'

const SERVICE = '99e7be30-0001-4c6b-98a2-70fcb3471a72'
const WRITE = '99e7be30-0002-4c6b-98a2-70fcb3471a72'
const NOTIFY = '99e7be30-0003-4c6b-98a2-70fcb3471a72'

// Only the Web Bluetooth surface used here; no additional runtime dependency.
interface Characteristic extends EventTarget {
  readonly value?: DataView
  startNotifications(): Promise<Characteristic>
  writeValueWithoutResponse(data: Uint8Array<ArrayBuffer>): Promise<void>
}
interface GattServer {
  readonly connected: boolean
  connect(): Promise<GattServer>
  disconnect(): void
  getPrimaryService(uuid: string): Promise<{ getCharacteristic(uuid: string): Promise<Characteristic> }>
}
interface BrowserDevice extends EventTarget {
  readonly id: string
  readonly name?: string
  readonly gatt?: GattServer
  watchAdvertisements?(options?: { signal?: AbortSignal }): Promise<void>
}
interface Bluetooth {
  requestDevice(options: {
    filters: ({ services: string[] } | { namePrefix: string } |
      { manufacturerData: { companyIdentifier: number; dataPrefix: Uint8Array }[] })[]
    optionalServices: string[]
    optionalManufacturerData: number[]
  }): Promise<BrowserDevice>
}

interface AdvertisementEvent extends Event {
  readonly manufacturerData: Map<number, DataView>
}

export class BluetoothError extends Error {}

export function bluetoothUnavailable(): string | null {
  if (!globalThis.isSecureContext) return t.bluetooth.secure
  if (!(navigator as Navigator & { bluetooth?: Bluetooth }).bluetooth) return t.bluetooth.unsupported
  return null
}

function bluetoothFailure(error: unknown): BluetoothError {
  const name = errorName(error)
  if (name === 'SecurityError' || name === 'NotAllowedError') return new BluetoothError(t.bluetooth.permission)
  return new BluetoothError(t.bluetooth.failed)
}

function errorName(error: unknown): string {
  return error !== null && typeof error === 'object' && 'name' in error ? String(error.name) : ''
}

interface BrowserSession { close(): void }
const sessions = new Map<string, BrowserSession>()

/** Called directly from a click: requestDevice must run before any network await. */
export async function requestBrowserSensor(): Promise<BrowserSession | null> {
  const unavailable = bluetoothUnavailable()
  if (unavailable) throw new BluetoothError(unavailable)
  const bluetooth = (navigator as Navigator & { bluetooth: Bluetooth }).bluetooth
  let device: BrowserDevice
  try {
    device = await bluetooth.requestDevice({
      filters: [
        { services: [SERVICE] },
        { namePrefix: 'RFBL_' },
        { namePrefix: 'MRBL_' },
        // Some MS605s advertise only their manufacturer signature, without the service UUID.
        { manufacturerData: [{ companyIdentifier: 0xffff, dataPrefix: new Uint8Array([0xc0]) }] },
      ],
      optionalServices: [SERVICE],
      optionalManufacturerData: [0xffff],
    })
  } catch (error) {
    if (errorName(error) === 'NotFoundError') return null // chooser cancelled
    throw bluetoothFailure(error)
  }
  const existing = sessions.get(device.id)
  if (existing) return existing
  if (!device.gatt) throw new BluetoothError(t.bluetooth.failed)
  const manufacturerData = await readManufacturerData(device)
  return openBridge(device, manufacturerData)
}

async function readManufacturerData(device: BrowserDevice): Promise<number[] | undefined> {
  if (!device.watchAdvertisements) return undefined
  const controller = new AbortController()
  let timer: ReturnType<typeof setTimeout> | undefined
  let finish: (data?: number[]) => void = () => {}
  const received = new Promise<number[] | undefined>((resolve) => {
    finish = resolve
  })
  const onAdvertisement = (event: Event) => {
    const value = (event as AdvertisementEvent).manufacturerData?.get(0xffff)
    if (!value || value.byteLength > 31) return
    finish(Array.from(new Uint8Array(value.buffer, value.byteOffset, value.byteLength)))
  }
  device.addEventListener('advertisementreceived', onAdvertisement)
  timer = setTimeout(() => finish(), 3_000)
  try {
    try {
      void device.watchAdvertisements({ signal: controller.signal }).catch(() => finish())
    } catch {
      finish()
    }
    return await received
  } finally {
    if (timer !== undefined) clearTimeout(timer)
    device.removeEventListener('advertisementreceived', onAdvertisement)
    controller.abort()
  }
}

function openBridge(device: BrowserDevice, manufacturerData?: number[]): Promise<BrowserSession> {
  return new Promise((resolve, reject) => {
    const scheme = location.protocol === 'https:' ? 'wss' : 'ws'
    const socket = new WebSocket(`${scheme}://${location.host}/ws/ble`)
    let closed = false
    let ready = false
    let intentionalDisconnect = false
    let write: Characteristic | null = null
    let notify: Characteristic | null = null
    let operationTimer: ReturnType<typeof setTimeout> | null = null
    let operations = Promise.resolve()
    const send = (message: unknown) => {
      if (!closed && socket.readyState === 1) socket.send(JSON.stringify(message))
    }
    const onNotify = () => {
      const value = notify?.value
      if (value) send({ type: 'notify', data: Array.from(new Uint8Array(value.buffer, value.byteOffset, value.byteLength)) })
    }
    const detachNotify = () => {
      notify?.removeEventListener('characteristicvaluechanged', onNotify)
      notify = write = null
    }
    const onDisconnected = () => {
      detachNotify()
      if (!intentionalDisconnect) send({ type: 'disconnected' })
    }
    const close = () => {
      if (closed) return
      closed = true
      clearTimeout(timer)
      if (operationTimer !== null) clearTimeout(operationTimer)
      unsubscribe()
      sessions.delete(device.id)
      detachNotify()
      device.removeEventListener('gattserverdisconnected', onDisconnected)
      window.removeEventListener('pagehide', close)
      device.gatt?.disconnect()
      socket.close()
      if (!ready) reject(new BluetoothError(t.bluetooth.serverLost))
    }
    const session = { close }
    const timer = setTimeout(close, 10_000)
    const unsubscribe = useStore.subscribe((state) => { if (state.conn === 'unauthorized') close() })
    const ensureOpen = () => {
      if (closed || !device.gatt?.connected) {
        // A timed-out native connect may settle after the user selected this device again.
        // Its cleanup must not disconnect the GATT link now owned by the newer bridge.
        const owner = sessions.get(device.id)
        if (!owner || owner === session) device.gatt?.disconnect()
        throw new BluetoothError(t.bluetooth.failed)
      }
    }
    device.addEventListener('gattserverdisconnected', onDisconnected)
    window.addEventListener('pagehide', close)
    socket.onopen = () => send({
      type: 'hello',
      name: device.name ?? null,
      ...(manufacturerData === undefined ? {} : { manufacturer_data: manufacturerData }),
    })
    socket.onerror = close
    socket.onclose = (event) => {
      if (event.code === 4401 || event.code === 4403) useStore.getState().setUnauthorized()
      close()
    }
    socket.onmessage = (event) => {
      let message: { type?: string; id?: number; op?: string; data?: number[] }
      try {
        if (typeof event.data !== 'string' || event.data.length > 8192) throw new Error('invalid message')
        message = JSON.parse(event.data)
        if (!message || typeof message !== 'object') throw new Error('invalid message')
      } catch { close(); return }
      if (message.type === 'ready' && !ready) {
        ready = true
        clearTimeout(timer)
        sessions.set(device.id, session)
        resolve(session)
        return
      }
      const { id, op, data } = message
      if (message.type !== 'request' || !Number.isSafeInteger(id) || (id ?? 0) <= 0 ||
        !['connect', 'start_notify', 'write', 'disconnect'].includes(op ?? '') ||
        (op === 'write' && (!Array.isArray(data) || !data.length || data.length > 512 ||
          data.some((byte) => !Number.isInteger(byte) || byte < 0 || byte > 255)))) {
        close()
        return
      }
      // Keep the server's chunk order even if several requests arrive in one event-loop turn.
      operations = operations.then(async () => {
        if (closed) return
        // Native GATT promises cannot be cancelled. Close the whole bridge if one stalls,
        // so later requests cannot queue forever and the user can select the sensor again.
        operationTimer = setTimeout(close, 10_000)
        try {
          if (op === 'connect') {
            detachNotify()
            const server = await device.gatt!.connect()
            ensureOpen()
            const service = await server.getPrimaryService(SERVICE)
            write = await service.getCharacteristic(WRITE)
            notify = await service.getCharacteristic(NOTIFY)
            ensureOpen()
            intentionalDisconnect = false
          } else if (op === 'start_notify') {
            ensureOpen()
            if (!notify) throw new BluetoothError(t.bluetooth.failed)
            notify.addEventListener('characteristicvaluechanged', onNotify)
            await notify.startNotifications()
          } else if (op === 'write') {
            ensureOpen()
            if (!write) throw new BluetoothError(t.bluetooth.failed)
            await write.writeValueWithoutResponse(new Uint8Array(data!))
          } else {
            intentionalDisconnect = true
            detachNotify()
            device.gatt!.disconnect()
          }
          send({ type: 'result', id })
        } catch (error) {
          send({ type: 'result', id, error: bluetoothFailure(error).message })
        } finally {
          if (operationTimer !== null) clearTimeout(operationTimer)
          operationTimer = null
        }
      })
    }
  })
}
