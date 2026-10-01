// Synthetic values only (public repo): simulator Device IDs b"SIM605" + n and locally administered addresses.
import type {
  BatchView,
  CalibrationJobView,
  LiveData,
  LiveInfo,
  LiveZone,
  PendingView,
  RegistryInfo,
  SensorView,
  ServerMessage,
  SiteView,
  StateSnapshot,
} from '../api/types'
import type { AppState } from '../store/reducer'
import { initialState } from '../store/reducer'

const hex2 = (n: number) => n.toString(16).padStart(2, '0')

export const deviceId = (n: number) => `53494d36303500${hex2(n)}`
export const address = (n: number) => `02:00:00:00:00:${hex2(n)}`
export const bleName = (n: number) => `MRBL_SIM${String(n).padStart(2, '0')}`

export const SITE_A: SiteView = { site_id: 'lab-a', name: 'Lab A' }
export const SITE_B: SiteView = { site_id: 'lab-b', name: 'Lab B' }

export const NOW_S = 1_790_000_000 // fixed epoch seconds for deterministic tests

export function live(n: number, patch: Partial<LiveInfo> = {}): LiveInfo {
  return {
    address: address(n),
    name: bleName(n),
    link: 'connected',
    busy: null,
    lost_reason: '',
    battery_pct: 87,
    firmware: '1.2.3',
    light_lux: 120,
    gathered_at: NOW_S - 60 + n,
    ...patch,
  }
}

export function registry(site: SiteView, alias: string, patch: Partial<RegistryInfo> = {}): RegistryInfo {
  return {
    site_id: site.site_id,
    alias,
    location: '',
    notes: '',
    last_seen: null,
    battery_pct: null,
    ...patch,
  }
}

export function sensor(n: number, patch: Partial<SensorView> = {}): SensorView {
  return {
    device_id: deviceId(n),
    registry: null,
    live: null,
    last_calibration: null,
    last_snapshot: null,
    ...patch,
  }
}

export function pending(site: SiteView, alias: string, n: number): PendingView {
  return { site_id: site.site_id, alias, address: address(n), source: 'sensor_info_example.yaml' }
}

export function stateSnapshot(patch: Partial<StateSnapshot> = {}): StateSnapshot {
  return {
    seq: 10,
    server: { version: '0.0.0', lan: false, sim: { count: 3, speed: 20 } },
    gather: { gathering: false, connecting: [] },
    sites: [SITE_A],
    sensors: [],
    pending: [],
    batch: null,
    ...patch,
  }
}

export function snapshotMsg(patch: Partial<StateSnapshot> = {}): ServerMessage {
  const data = stateSnapshot(patch)
  return { type: 'snapshot', seq: data.seq, ts: NOW_S, data }
}

/** A dashboard with every kind of entry: connected, lost, offline, unregistered, pending. */
export function richSensors(): SensorView[] {
  return [
    sensor(1, { registry: registry(SITE_A, '센서 1', { location: '북쪽 벽' }), live: live(1) }),
    sensor(2, { registry: registry(SITE_A, '센서 2'), live: live(2, { link: 'lost', lost_reason: 'idle' }) }),
    sensor(3, {
      registry: registry(SITE_A, '센서 3', { last_seen: '2026-09-30T08:00:00Z', battery_pct: 64 }),
      last_calibration: { timestamp: '2026-09-28T08:00:00Z', sensitivity: 2, detect_mode: 1 },
    }),
    sensor(4, { registry: registry(SITE_B, '창가 센서') }),
    sensor(5, { live: live(5) }),
  ]
}

/** Store state as the WS would leave it after `snapshot`. */
export function storeState(patch: Partial<StateSnapshot> = {}): Partial<AppState> {
  const snap = stateSnapshot(patch)
  return {
    ...initialState,
    conn: 'open',
    lastSeq: snap.seq,
    server: snap.server,
    gather: snap.gather,
    sites: Object.fromEntries(snap.sites.map((s) => [s.site_id, s])),
    sensors: Object.fromEntries(snap.sensors.map((s) => [s.device_id, s])),
    pending: snap.pending,
    batch: snap.batch,
  }
}

// -- M3 -----------------------------------------------------------------------------------

export const BATCH_ID = 'b-0001'

/** Seven zones 0.8 m apart, the simulator's tag53 (G23). */
export function liveZone(index: number, patch: Partial<LiveZone> = {}): LiveZone {
  return {
    index,
    distance_m: 0.8 * (index + 1),
    enabled: true,
    trigger_active: false,
    trigger: 40,
    trigger_threshold: 55,
    maintain: 20,
    maintain_threshold: 30,
    ...patch,
  }
}

/** Z0 above its threshold (trigger flag on), Z2 a negative calibration threshold, Z6 off. */
export function liveData(n: number, patch: Partial<LiveData> = {}): LiveData {
  return {
    device_id: deviceId(n),
    at: NOW_S,
    pir: true,
    sub_sensor_presence: [true, false, false],
    zones: [
      liveZone(0, { trigger: 64, trigger_threshold: 60, trigger_active: true, maintain: 22, maintain_threshold: 30 }),
      liveZone(1, { trigger: 41, trigger_threshold: 55, maintain: 30, maintain_threshold: 30 }),
      liveZone(2, { trigger: -10, trigger_threshold: -33, maintain: 5, maintain_threshold: 0 }),
      liveZone(3),
      liveZone(4),
      liveZone(5),
      liveZone(6, { enabled: false }),
    ],
    ...patch,
  }
}

const pairs = (trigger: number[], maintain = 30) => trigger.map((t) => ({ trigger: t, maintain }))

export function job(n: number, patch: Partial<CalibrationJobView> = {}): CalibrationJobView {
  return {
    batch_id: BATCH_ID,
    device_id: deviceId(n),
    attempt: 1,
    state: 'idle',
    started: false,
    elapsed_s: null,
    error: null,
    detail: '',
    before: null,
    after: null,
    history_saved: false,
    retryable: false,
    ...patch,
  }
}

export function succeededJob(n: number, patch: Partial<CalibrationJobView> = {}): CalibrationJobView {
  return job(n, {
    state: 'succeeded',
    started: true,
    elapsed_s: 1.8,
    before: [{ trigger: 70, maintain: 30 }, ...pairs([62, 55, 55, 55, 55, 55])],
    after: [{ trigger: 64, maintain: 28 }, ...pairs([66, 55, 55, 55, 55, 55])],
    history_saved: true,
    ...patch,
  })
}

/** Three sensors: done -> succeeded, lost while learning, succeeded. */
export function batchView(patch: Partial<BatchView> = {}): BatchView {
  const ids = [1, 2, 3].map(deviceId)
  return {
    batch_id: BATCH_ID,
    state: 'done',
    round: 1,
    start: 'now',
    fire_at: NOW_S,
    created_at: NOW_S,
    expected_s: 180,
    presence_override: false,
    device_ids: ids,
    round_ids: ids,
    jobs: [succeededJob(1), job(2, { state: 'lost', started: true, elapsed_s: 40, retryable: true }), succeededJob(3)],
    ...patch,
  }
}

/** Three registered, connected sensors in Lab A. */
export function calibSensors(patch: (n: number) => Partial<SensorView> = () => ({})): SensorView[] {
  return [1, 2, 3].map((n) => sensor(n, { registry: registry(SITE_A, `센서 ${n}`), live: live(n), ...patch(n) }))
}
