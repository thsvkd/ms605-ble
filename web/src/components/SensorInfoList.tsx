import type { SensorView } from '../api/types'
import { formatLux } from '../format'
import { t } from '../strings'
import { BatteryIndicator } from './BatteryIndicator'
import { RelativeTime } from './RelativeTime'
import styles from './detail.module.css'

export function SensorInfoList({ sensor }: { sensor: SensorView }) {
  const { live, registry, last_calibration: cal, last_snapshot: snap } = sensor
  const none = t.detail.none
  return (
    <section className={styles.card} aria-labelledby="info-title">
      <h2 id="info-title" className={styles.cardTitle}>
        {t.detail.info}
      </h2>
      <dl className={styles.info}>
        <dt>{t.detail.deviceId}</dt>
        <dd className="mono">{sensor.device_id}</dd>
        <dt>{t.detail.address}</dt>
        <dd className="mono">{live?.address ?? none}</dd>
        <dt>{t.detail.firmware}</dt>
        <dd>{live?.firmware ?? none}</dd>
        <dt>{t.detail.battery}</dt>
        <dd>
          <BatteryIndicator sensor={sensor} />
          {live?.battery_pct == null && registry?.battery_pct == null && none}
        </dd>
        <dt>{t.detail.light}</dt>
        <dd>{formatLux(live?.light_lux ?? null)}</dd>
        <dt>{t.detail.lastSeen}</dt>
        <dd>{registry?.last_seen ? <RelativeTime value={registry.last_seen} /> : t.time.neverSeen}</dd>
        <dt>{t.detail.lastCalibration}</dt>
        <dd>{cal ? <RelativeTime value={cal.timestamp} /> : t.calibration.none}</dd>
        <dt>{t.detail.lastSnapshot}</dt>
        <dd>{snap ? <RelativeTime value={snap.taken_at} /> : none}</dd>
      </dl>
    </section>
  )
}
