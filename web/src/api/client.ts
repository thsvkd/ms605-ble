import { useStore } from '../store/store'
import type {
  ErrorCode,
  GatherStatus,
  ImportResult,
  SensorCreate,
  SensorInfoImport,
  SensorUpdate,
  SensorView,
  SiteView,
} from './types'

export class ApiRequestError extends Error {
  readonly status: number
  readonly code: ErrorCode

  constructor(status: number, code: ErrorCode, message: string) {
    super(message)
    this.name = 'ApiRequestError'
    this.status = status
    this.code = code
  }
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const res = await fetch(path, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
    credentials: 'same-origin',
  })
  if (!res.ok) {
    if (res.status === 401) useStore.getState().setUnauthorized()
    let code: ErrorCode = 'internal'
    let message = `HTTP ${res.status}`
    try {
      const parsed = (await res.json()) as { error?: { code?: ErrorCode; message?: string } }
      if (parsed.error?.code) {
        code = parsed.error.code
        message = parsed.error.message ?? message
      }
    } catch {
      // body is not JSON (e.g. the Host middleware's plain-text 400)
    }
    throw new ApiRequestError(res.status, code, message)
  }
  if (res.status === 204) return undefined as T
  return (await res.json()) as T
}

const enc = encodeURIComponent

export const createSite = (name: string) => request<SiteView>('POST', '/api/sites', { name, site_id: null })

/** The site named `name` (trimmed, case-insensitive) if one exists, else a new one: no two sites share a name. */
export async function ensureSite(name: string): Promise<string> {
  const wanted = name.trim().toLowerCase()
  const found = Object.values(useStore.getState().sites).find((s) => s.name.trim().toLowerCase() === wanted)
  return found ? found.site_id : (await createSite(name.trim())).site_id
}

export const createSensor = (body: SensorCreate) => request<SensorView>('POST', '/api/sensors', body)

/** Only the fields to change: an absent field is left as it is on the server. */
export const updateSensor = (deviceId: string, body: Partial<SensorUpdate>) =>
  request<SensorView>('PATCH', `/api/sensors/${enc(deviceId)}`, body)

export const deleteSensor = (deviceId: string) => request<void>('DELETE', `/api/sensors/${enc(deviceId)}`)

export const startGather = () => request<GatherStatus>('POST', '/api/gather/start')

export const stopGather = () => request<GatherStatus>('POST', '/api/gather/stop')

/** `null` releases every session. */
export const release = (deviceIds: string[] | null) => request<void>('POST', '/api/release', { device_ids: deviceIds })

export const importSensorInfo = (body: SensorInfoImport) =>
  request<ImportResult>('POST', '/api/import/sensor-info', body)

export const simPress = (index: number) => request<void>('POST', `/api/sim/press/${index}`)

export const simPressAll = () => request<void>('POST', '/api/sim/press-all')

export const simDrop = (index: number) => request<void>('POST', `/api/sim/drop/${index}`)
