import { Hourglass } from 'lucide-react'
import { memo } from 'react'
import { Link } from 'wouter'
import { useNow } from '../hooks/useNow'
import { sensorStatus } from '../status'
import { sensorName, useStore } from '../store/store'
import { t } from '../strings'
import { PresenceChips } from './PresenceChips'
import { StatusBadge } from './StatusBadge'
import { ZoneList } from './ZoneMeterRow'
import styles from './live.module.css'

/** One watched sensor on the monitor. Subscribes to its own entries only. */
export const SensorLiveCard = memo(function SensorLiveCard({ deviceId }: { deviceId: string }) {
  const sensor = useStore((s) => s.sensors[deviceId])
  const frame = useStore((s) => s.live[deviceId])
  const gathering = useStore((s) => s.gather.gathering)
  const now = useNow()
  if (!sensor?.live) return null

  const status = sensorStatus(sensor, gathering, now)
  const connected = sensor.live.link === 'connected'
  const location = sensor.registry?.location
  const titleId = `live-${deviceId}`

  return (
    <section className={styles.card} data-stale={!connected} aria-labelledby={titleId}>
      <div className={styles.cardHead}>
        <h2 id={titleId} className={styles.cardTitle}>
          <Link href={`/sensors/${deviceId}`} className={styles.titleLink}>
            {sensorName(sensor, deviceId)}
          </Link>
          {location && <span className={styles.cardSub}> · {location}</span>}
        </h2>
        <StatusBadge status={status} />
      </div>
      {!connected && status.hint && <p className={styles.hint}>{status.hint}</p>}
      {connected && sensor.live.busy === 'calibration' && (
        <p className={styles.calibrating}>
          <Hourglass size={16} aria-hidden />
          {t.live.calibrating}
        </p>
      )}
      {!connected && <p className={styles.note}>{t.live.stale}</p>}
      {frame ? (
        <>
          <PresenceChips frame={frame} />
          <ZoneList zones={frame.zones} />
        </>
      ) : (
        connected && <p className={styles.note}>{t.live.waiting}</p>
      )}
    </section>
  )
})
