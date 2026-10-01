// Synthetic values only (public repo): simulator Device IDs b"SIM605" + n and locally administered addresses.
import type {
  LiveInfo,
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
  }
}
