// Synthetic values only (public repo): simulator Device IDs b"SIM605" + n and locally administered addresses.
import type {
  ApplyItemView,
  ApplyJobView,
  BatchView,
  CalibrationHistory,
  CalibrationJobView,
  Change,
  ConfigView,
  DraftPreview,
  LiveData,
  LiveInfo,
  LiveZone,
  PendingView,
  ProfileView,
  RegistryInfo,
  SensorPreview,
  SensorView,
  ServerMessage,
  SiteView,
  SnapshotList,
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
    mac: null,
    name: bleName(n),
    link: 'connected',
    auto_reconnect: false,
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
    config_rev: 0,
    ...patch,
  }
}

export function pending(site: SiteView, alias: string, n: number): PendingView {
  return { site_id: site.site_id, alias, address: address(n), source: 'sensor_info_example.yaml' }
}

export function stateSnapshot(patch: Partial<StateSnapshot> = {}): StateSnapshot {
  return {
    seq: 10,
    server: { ble_transport: 'browser', version: '0.0.0', lan: false, sim: { count: 3, speed: 20 } },
    gather: { gathering: false, connecting: [] },
    sites: [SITE_A],
    sensors: [],
    pending: [],
    batch: null,
    apply: null,
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
    apply: snap.apply,
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

// -- M4 (docs/GUI_API.md 15.10.2) -----------------------------------------------------------

export const APPLY_ID = 'a0000000000000000000000000000001'
export const SNAP_1 = '20261001T080000000000Z'
export const SNAP_2 = '20260930T120000000000Z'

/** The simulator's MEDIUM preset shape, with Z0 trigger 60 (the drag tests) and Z6 off. */
export function profile(patch: Partial<ProfileView> = {}): ProfileView {
  return {
    sensitivity: 2,
    detect_mode: 1,
    zone_enable: [true, true, true, true, true, true, false],
    zone_thresholds: [
      { trigger: 60, maintain: 30 },
      { trigger: 55, maintain: 30 },
      { trigger: 50, maintain: 28 },
      { trigger: 50, maintain: 28 },
      { trigger: 45, maintain: 25 },
      { trigger: 45, maintain: 25 },
      { trigger: 40, maintain: 20 },
    ],
    subsensor_zones: [[0, 1, 2], [3, 4], [5, 6]],
    subsensor_timing: [
      [5, 30],
      [5, 30],
      [5, 30],
    ],
    subsensor_enable: [true, true, true],
    dnd: false,
    ...patch,
  }
}

export function configView(n: number, patch: Partial<ConfigView> = {}): ConfigView {
  return {
    device_id: deviceId(n),
    read_at: NOW_S,
    config_rev: 0,
    distances_m: [0.8, 1.6, 2.4, 3.2, 4.0, 4.8, 5.6],
    profile: profile(),
    ...patch,
  }
}

export function change(patch: Partial<Change> = {}): Change {
  return { section: 'zone_thresholds', index: 0, part: 'trigger', before: 60, after: 65, risks: [], ...patch }
}

export function sensorPreview(n: number, patch: Partial<SensorPreview> = {}): SensorPreview {
  return {
    device_id: deviceId(n),
    config_rev: 0,
    error: null,
    before: profile(),
    after: profile(),
    changes: [change()],
    risks: [],
    ...patch,
  }
}

/** One sensor, no risk. */
export function previewPlain(): DraftPreview {
  return { kind: 'apply', checked_at: NOW_S, items: [sensorPreview(1)], risks: [], source_rev: null }
}

/** Absolute thresholds on two sensors (absolute_overwrite + a large change), the third cannot be read. */
export function previewRisky(): DraftPreview {
  const risky = (n: number) =>
    sensorPreview(n, {
      changes: [
        change({ before: 95, after: 60, risks: ['absolute_overwrite', 'large_change'] }),
        change({ part: 'maintain', before: 40, after: 40 + n, risks: ['absolute_overwrite'] }),
      ],
      risks: ['absolute_overwrite', 'large_change'],
    })
  return {
    kind: 'apply',
    checked_at: NOW_S,
    items: [
      risky(1),
      risky(2),
      sensorPreview(3, { error: 'busy: read', before: null, after: null, changes: [], risks: [] }),
    ],
    risks: ['absolute_overwrite', 'large_change'],
    source_rev: null,
  }
}

export function applyItem(n: number, patch: Partial<ApplyItemView> = {}): ApplyItemView {
  return {
    device_id: deviceId(n),
    state: 'verified',
    restore: null,
    snapshot: SNAP_1,
    applied: ['zone_thresholds'],
    skipped: [],
    mismatched: [],
    error: null,
    finished_at: NOW_S + 2,
    ...patch,
  }
}

export function applyJob(patch: Partial<ApplyJobView> = {}): ApplyJobView {
  return {
    apply_id: APPLY_ID,
    kind: 'apply',
    state: 'done',
    created_at: NOW_S,
    source: null,
    sections: ['zone_thresholds'],
    items: [applyItem(1), applyItem(2), applyItem(3)],
    ...patch,
  }
}

export const jobRunning = () =>
  applyJob({
    state: 'running',
    items: [
      applyItem(1),
      applyItem(2, { state: 'applying', snapshot: null, applied: [], finished_at: null }),
      applyItem(3, { state: 'queued', snapshot: null, applied: [], finished_at: null }),
    ],
  })

/** verified · failed while writing · partial. */
export const jobPartial = () =>
  applyJob({
    items: [
      applyItem(1),
      applyItem(2, { state: 'failed', applied: [], error: 'device returned error status 5' }),
      applyItem(3, { state: 'partial', mismatched: ['zone_thresholds'] }),
    ],
  })

export const jobAllVerified = () => applyJob()

export function snapshotList(n: number): SnapshotList {
  return {
    device_id: deviceId(n),
    snapshots: [
      { name: SNAP_1, taken_at: '2026-10-01T08:00:00Z', reason: 'apply', sections: ['zone_thresholds', 'sensitivity'] },
      { name: SNAP_2, taken_at: '2026-09-30T12:00:00Z', reason: 'rollback', sections: ['zone_thresholds'] },
    ],
  }
}

export function calibrationHistoryOf(n: number): CalibrationHistory {
  const zones = Array.from({ length: 7 }, (_, i) => ({ index: i, distance_m: 0.8 * (i + 1), trigger: 60 - i, maintain: 30 }))
  return {
    device_id: deviceId(n),
    records: [
      { timestamp: '2026-09-29T14:10:00Z', device_name: bleName(n), sensitivity: 4, detect_mode: 1, zones },
      { timestamp: '2026-09-20T09:31:00Z', device_name: bleName(n), sensitivity: 2, detect_mode: 2, zones },
    ],
  }
}
