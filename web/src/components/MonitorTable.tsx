import { CircleHelp, Eye, Hourglass, Radio, Unplug, UserRound } from 'lucide-react'
import { memo, useMemo } from 'react'
import { Link } from 'wouter'
import type { LiveData, LiveZone } from '../api/types'
import { useNow } from '../hooks/useNow'
import { meter } from '../meter'
import { presenceOf, presenceTally } from '../presence'
import { sensorStatus } from '../status'
import { sensorName, useStore } from '../store/store'
import { t } from '../strings'
import { SignalChip, SubBoxes } from './PresenceSignals'
import styles from './monitor.module.css'

const ZONES = [0, 1, 2, 3, 4, 5, 6] // the device's seven radar zones (LiveData.zones)

/**
 * "N 재실 / M대 · N PIR 감지 · N RF 감지", over the watched sensors' latest frames. Only a connected sensor
 * counts: the last frame of a lost or released one is history, so it shows as "연결 끊김" instead.
 */
export function MonitorSummary({ ids }: { ids: readonly string[] }) {
  const live = useStore((s) => s.live)
  const sensors = useStore((s) => s.sensors)
  const c = useMemo(
    () => presenceTally(ids.map((id) => ({ frame: live[id], connected: sensors[id]?.live?.link === 'connected' }))),
    [ids, live, sensors],
  )
  const stats = [
    { key: 'present', n: c.present, tone: 'present', Icon: UserRound, label: t.monitor.summaryPresent(c.total) },
    { key: 'pir', n: c.pir, tone: 'hit', Icon: Eye, label: t.monitor.summaryPir },
    { key: 'rf', n: c.rf, tone: 'hit', Icon: Radio, label: t.monitor.summaryRf },
  ]
  return (
    <ul className={styles.summary} aria-label={t.monitor.summaryLabel}>
      {stats.map(({ key, n, tone, Icon, label }) => (
        <li key={key} className={styles.stat} data-tone={n > 0 ? tone : 'none'}>
          <Icon size={16} aria-hidden />
          <b>{n}</b>
          <span>{label}</span>
        </li>
      ))}
      {c.unknown > 0 && (
        <li className={styles.stat} data-tone="unknown">
          <CircleHelp size={16} aria-hidden />
          <b>{c.unknown}</b>
          <span>{t.monitor.summaryUnknown}</span>
        </li>
      )}
      {c.offline > 0 && (
        <li className={styles.stat} data-tone="offline">
          <Unplug size={16} aria-hidden />
          <b>{c.offline}</b>
          <span>{t.monitor.summaryOffline}</span>
        </li>
      )}
    </ul>
  )
}

const MIXED = '*'

/**
 * Zone distances come from each device's own config, so they go in the column heads only when every watched
 * frame agrees. A string key, so the heads do not redraw per frame: '' = no frame yet, MIXED = they differ.
 */
function useZoneDistances(ids: readonly string[]): string {
  return useStore((s) => {
    let shared = ''
    for (const id of ids) {
      const f = s.live[id]
      if (!f) continue
      const k = f.zones.map((z) => z.distance_m.toFixed(1)).join(',')
      if (!shared) shared = k
      else if (k !== shared) return MIXED
    }
    return shared
  })
}

/**
 * One sensor per row, Z0..Z6 per column. The row header holds only the name and its link state (a screen
 * reader repeats it before every cell); PIR / RF / 재실 sit in their own cell beside it. Both stay put while
 * the zones scroll sideways.
 */
export function MonitorTable({ ids }: { ids: readonly string[] }) {
  const key = useZoneDistances(ids)
  const mixed = key === MIXED
  const dist = key && !mixed ? key.split(',') : null
  return (
    <div className={styles.scroll} role="region" aria-label={t.monitor.scrollLabel} tabIndex={0}>
      <table className={styles.table}>
        <caption className="visually-hidden">{t.monitor.tableLabel}</caption>
        <thead>
          <tr>
            <th scope="col" className={styles.corner}>
              {t.monitor.colSensor}
            </th>
            <th scope="col" className={styles.cornerSignals}>
              {t.monitor.colSignals}
            </th>
            {ZONES.map((i) => (
              <th key={i} scope="col" className={styles.zoneHead}>
                Z{i}
                {dist?.[i] && <span className={styles.headSub}>{dist[i]} m</span>}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {ids.map((id) => (
            <MonitorRow key={id} deviceId={id} cellDistance={mixed} />
          ))}
        </tbody>
      </table>
    </div>
  )
}

const MonitorRow = memo(function MonitorRow({ deviceId, cellDistance }: { deviceId: string; cellDistance: boolean }) {
  const sensor = useStore((s) => s.sensors[deviceId])
  const frame = useStore((s) => s.live[deviceId])
  const gathering = useStore((s) => s.gather.gathering)
  const now = useNow()
  if (!sensor?.live) return null

  const status = sensorStatus(sensor, gathering, now)
  const connected = sensor.live.link === 'connected'
  const calibrating = connected && sensor.live.busy === 'calibration'
  const location = sensor.registry?.location
  const p = presenceOf(frame)

  return (
    <tr className={styles.row} data-stale={!connected} data-present={connected && p.present === true}>
      <th scope="row" className={styles.name}>
        <Link href={`/sensors/${deviceId}`} className={styles.nameLink}>
          {sensorName(sensor, deviceId)}
        </Link>
        {location && <span className={styles.sub}>{location}</span>}
        {!connected && (
          <span className={styles.warn}>
            {status.label}
            {status.hint && <span className={styles.hint}>{status.hint}</span>}
            {frame && <span className={styles.hint}>{t.live.stale}</span>}
          </span>
        )}
        {calibrating && (
          <span className={styles.calibrating}>
            <Hourglass size={12} aria-hidden />
            <span>
              {status.label}
              <span className={styles.hint}>{t.live.calibrating}</span>
            </span>
          </span>
        )}
        {connected && !frame && <span className={styles.sub}>{t.live.waiting}</span>}
      </th>
      <td className={styles.signals}>
        <span className={styles.signalLine}>
          <SignalChip kind="pir" value={p.pir} short />
          <SignalChip kind="rf" value={p.rf} short />
        </span>
        <span className={styles.signalLine}>
          <SignalChip kind="presence" value={p.present} short />
          <SubBoxes subs={p.subs} />
        </span>
      </td>
      {ZONES.map((i) => (
        <ZoneCell key={i} index={i} frame={frame} showDistance={cellDistance} />
      ))}
    </tr>
  )
})

function ZoneCell(props: { index: number; frame: LiveData | undefined; showDistance: boolean }) {
  const { index, frame, showDistance } = props
  const z = frame?.zones[index]
  if (!z) {
    return (
      <td className={styles.cell} data-empty>
        <span className={styles.muted} aria-hidden>
          —
        </span>
        <span className="visually-hidden">{t.monitor.noValue}</span>
      </td>
    )
  }
  const where = t.live.zone(z.index, z.distance_m)
  // this sensor's own distance, when the column head cannot give one for every row
  const distance = showDistance && <span className={styles.cellDistance}>{t.monitor.distance(z.distance_m)}</span>
  if (!z.enabled) {
    return (
      <td className={styles.cell} data-off title={where}>
        {distance}
        <span className={styles.muted}>{t.live.zoneOff}</span>
      </td>
    )
  }
  const triggerOver = z.trigger > z.trigger_threshold
  const maintainOver = z.maintain > z.maintain_threshold
  return (
    <td
      className={styles.cell}
      data-over={triggerOver ? 'trigger' : maintainOver ? 'maintain' : undefined}
      title={where}
    >
      {distance}
      <ZoneMeters zone={z} />
    </td>
  )
}

function ZoneMeters({ zone: z }: { zone: LiveZone }) {
  return (
    <>
      <MiniMeter
        tag={t.monitor.tagTrigger}
        label={t.live.trigger}
        value={z.trigger}
        threshold={z.trigger_threshold}
        hot={z.trigger_active}
      />
      <MiniMeter
        tag={t.monitor.tagMaintain}
        label={t.live.maintain}
        value={z.maintain}
        threshold={z.maintain_threshold}
        hot={z.maintain > z.maintain_threshold}
      />
    </>
  )
}

/** Meter.tsx at table size: the same meter() cells, fixed tick and red-iff-over rule, with cur/thr. */
function MiniMeter(props: { tag: string; label: string; value: number; threshold: number; hot: boolean }) {
  const { tag, label, value, threshold, hot } = props
  const { cells, over } = meter(value, threshold)
  return (
    <div className={styles.mini} data-over={over}>
      <span className={styles.tag} aria-hidden>
        {tag}
      </span>
      <span className={styles.track} role="img" aria-label={t.live.meterAria(label, value, threshold, over)}>
        {cells.map((c, i) => (
          <i key={i} data-cell={c} />
        ))}
      </span>
      <span className={styles.value} data-hot={hot} aria-hidden>
        {value}
        <small>/{threshold}</small>
      </span>
    </div>
  )
}
