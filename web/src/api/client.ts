import { useStore } from '../store/store'
import { errorText, t } from '../strings'
import type {
  ApplyIn,
  ApplyJobView,
  BatchView,
  CalibrationHistory,
  CloneApplyIn,
  CloneIn,
  ConfigView,
  DeviceHistory,
  DeviceHistoryKind,
  DraftIn,
  DraftPreview,
  ErrorCode,
  GatherStatus,
  ImportResult,
  PreflightResult,
  RollbackApplyIn,
  RollbackIn,
  SensorCreate,
  SensorInfoImport,
  SensorUpdate,
  SensorView,
  SiteView,
  SnapshotDetail,
  SnapshotList,
  StartMode,
  TimeSyncResult,
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

// -- M3 (docs/GUI_API.md 14.8.7): responses only colour the form; the batch itself arrives over WS (G10)

export const preflight = (deviceIds: string[], windowS?: number) =>
  request<PreflightResult>(
    'POST',
    '/api/preflight',
    windowS === undefined ? { device_ids: deviceIds } : { device_ids: deviceIds, window_s: windowS },
  )

export interface StartBody {
  start: StartMode
  delay_s?: number
  /** ISO 8601 with an offset (toISOString's Z counts). */
  at?: string
}

export const createBatch = (body: StartBody & { device_ids: string[]; presence_override?: boolean }) =>
  request<BatchView>('POST', '/api/batches', body)

export const getBatch = (batchId: string) => request<BatchView>('GET', `/api/batches/${enc(batchId)}`)

export const cancelBatch = (batchId: string) => request<BatchView>('POST', `/api/batches/${enc(batchId)}/cancel`)

export const retryBatch = (batchId: string, body: StartBody & { device_ids?: string[] }) =>
  request<BatchView>('POST', `/api/batches/${enc(batchId)}/retry`, body)

// -- M4 (docs/GUI_API.md 15.9.15): previews, config, history and time sync are drawn as returned;
// an apply job's state arrives over WS `apply` (G10), the 202 only names "my" job.

export const getConfig = (deviceId: string) => request<ConfigView>('GET', `/api/sensors/${enc(deviceId)}/config`)
export const previewDraft = (body: DraftIn) => request<DraftPreview>('POST', '/api/drafts/preview', body)
export const applyDraft = (body: ApplyIn) => request<ApplyJobView>('POST', '/api/apply', body)
export const getApply = (applyId: string) => request<ApplyJobView>('GET', `/api/apply/${enc(applyId)}`)
export const listSnapshots = (deviceId: string) =>
  request<SnapshotList>('GET', `/api/sensors/${enc(deviceId)}/snapshots`)
export const getSnapshot = (deviceId: string, name: string) =>
  request<SnapshotDetail>('GET', `/api/sensors/${enc(deviceId)}/snapshots/${enc(name)}`)
export const previewRollback = (body: RollbackIn) => request<DraftPreview>('POST', '/api/rollback/preview', body)
export const rollback = (body: RollbackApplyIn) => request<ApplyJobView>('POST', '/api/rollback', body)
export const previewClone = (body: CloneIn) => request<DraftPreview>('POST', '/api/clone/preview', body)
export const clone = (body: CloneApplyIn) => request<ApplyJobView>('POST', '/api/clone', body)
export const timeSync = (deviceIds: string[]) =>
  request<TimeSyncResult>('POST', '/api/time-sync', { device_ids: deviceIds })
export const calibrationHistory = (deviceId: string) =>
  request<CalibrationHistory>('GET', `/api/sensors/${enc(deviceId)}/history`)
export const deviceHistory = (deviceId: string, kind: DeviceHistoryKind, detail = false) =>
  request<DeviceHistory>('GET', `/api/sensors/${enc(deviceId)}/device-history?kind=${kind}&detail=${detail}`)

/** The words for a failed request (any thrown value). */
export function failureText(e: unknown): string {
  return e instanceof ApiRequestError ? errorText(e.code, e.message) : t.error.internal
}
