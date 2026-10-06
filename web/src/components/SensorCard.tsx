import { memo } from 'react'
import { Link } from 'wouter'
import { relativeTime } from '../format'
import { useNow } from '../hooks/useNow'
import { presenceOf } from '../presence'
import { sensorStatus } from '../status'
import { useStore } from '../store/store'
import { useStrings } from '../strings'
import { BatteryIndicator } from './BatteryIndicator'
import { PresencePanel } from './PresenceSignals'
import { StatusBadge } from './StatusBadge'
import styles from './dashboard.module.css'

/**
 * A registered sensor. Subscribes to its own entry so one sensor's update redraws one card. While connected it
 * shows the device's 재실 call (the card's answer) apart from the raw PIR / RF chips; the screen holds the watch.
 */
export const SensorCard = memo(function SensorCard({ deviceId }: { deviceId: string }) {
  const t = useStrings()
  const sensor = useStore((s) => s.sensors[deviceId])
  const frame = useStore((s) => s.live[deviceId])
  const gathering = useStore((s) => s.gather.gathering)
  const now = useNow()
  if (!sensor?.registry) return null
  const status = sensorStatus(sensor, gathering, now)
  const { alias, location } = sensor.registry
  const cal = sensor.last_calibration
  const connected = sensor.live?.link === 'connected'
  return (
    <Link href={`/sensors/${deviceId}`} className={styles.card} data-kind={status.kind}>
      <div className={styles.cardHead}>
        <h3 className={styles.cardTitle}>{alias}</h3>
        <StatusBadge status={status} />
      </div>
      {status.hint && (
        <p className={styles.hint} data-kind={status.kind}>
          {status.hint}
        </p>
      )}
      <div className={styles.cardMeta}>
        {location && <span>{location}</span>}
        <BatteryIndicator sensor={sensor} />
      </div>
      <p className={styles.cardMeta}>
        <span>{cal ? t.calibration.last(relativeTime(cal.timestamp, now)) : t.calibration.none}</span>
      </p>
      {connected && (
        <div className={styles.presence}>
          <PresencePanel presence={presenceOf(frame)} />
        </div>
      )}
    </Link>
  )
})
