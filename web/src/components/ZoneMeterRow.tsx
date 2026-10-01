import type { LiveZone } from '../api/types'
import { t } from '../strings'
import { Meter } from './Meter'
import styles from './live.module.css'

/** One zone: trigger + maintain bars, or only the trigger bar when `compact`. */
export function ZoneMeterRow({ zone, compact }: { zone: LiveZone; compact?: boolean }) {
  const label = <span className={styles.zoneLabel}>{t.live.zone(zone.index, zone.distance_m)}</span>
  if (!zone.enabled) {
    return (
      <li className={`${styles.zone} ${styles.zoneOffRow}`}>
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
        compact={compact}
      />
      {!compact && (
        <Meter
          value={zone.maintain}
          threshold={zone.maintain_threshold}
          label={t.live.maintain}
          hot={zone.maintain > zone.maintain_threshold}
        />
      )}
    </li>
  )
}

export function ZoneList({ zones, compact }: { zones: LiveZone[]; compact?: boolean }) {
  return (
    <ul className={`${styles.zones} ${compact ? styles.zonesCompact : ''}`}>
      {!compact && (
        <li className={styles.zoneHead} aria-hidden>
          <span>{t.live.zoneHead}</span>
          <span>{t.live.trigger}</span>
          <span>{t.live.maintain}</span>
        </li>
      )}
      {zones.map((z) => (
        <ZoneMeterRow key={z.index} zone={z} compact={compact} />
      ))}
    </ul>
  )
}
