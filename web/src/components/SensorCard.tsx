import { memo } from 'react'
import { Link } from 'wouter'
import { useShallow } from 'zustand/react/shallow'
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
  // Radar levels arrive several times a second but this card only draws presence signals. Flatten S1..S3 so
  // Zustand can skip a render when a new frame carries the same displayed signal values.
  const presence = useStore(useShallow((s) => {
    const p = presenceOf(s.live[deviceId])
    return {
      pir: p.pir,
      rf: p.rf,
      present: p.present,
      subsKnown: p.subs !== null,
      s1: p.subs?.[0] ?? false,
      s2: p.subs?.[1] ?? false,
      s3: p.subs?.[2] ?? false,
    }
  }))
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
          <PresencePanel
            presence={{
              pir: presence.pir,
              rf: presence.rf,
              present: presence.present,
              subs: presence.subsKnown ? [presence.s1, presence.s2, presence.s3] : null,
            }}
          />
        </div>
      )}
    </Link>
  )
})
