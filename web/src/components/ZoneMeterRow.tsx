import type { LiveZone } from '../api/types'
import { t } from '../strings'
import { Meter } from './Meter'
import styles from './live.module.css'

/** One zone on the sensor detail strip: its label and the compact trigger bar. */
function ZoneMeterRow({ zone }: { zone: LiveZone }) {
  const label = <span className={styles.zoneLabel}>{t.live.zone(zone.index, zone.distance_m)}</span>
  if (!zone.enabled) {
    return (
      <li className={styles.zone}>
        {label}
        <span className={styles.zoneOff}>{t.live.zoneOff}</span>
      </li>
    )
  }
  return (
    <li className={styles.zone}>
      {label}
      <Meter
        value={zone.trigger}
        threshold={zone.trigger_threshold}
        label={t.live.trigger}
        hot={zone.trigger_active}
        compact
      />
    </li>
  )
}

export function ZoneList({ zones }: { zones: LiveZone[] }) {
  return (
    <ul className={styles.zones}>
      {zones.map((z) => (
        <ZoneMeterRow key={z.index} zone={z} />
      ))}
    </ul>
  )
}
