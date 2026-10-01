import { Activity, ArrowRight } from 'lucide-react'
import { Link } from 'wouter'
import { useLiveWatch } from '../hooks/useLiveWatch'
import { useStore } from '../store/store'
import { t } from '../strings'
import { PresenceChips } from './PresenceChips'
import { ZoneList } from './ZoneMeterRow'
import styles from './live.module.css'

/** Sensor detail: the trigger bars of one sensor, watched only while this page is open. */
export function LiveStrip({ deviceId }: { deviceId: string }) {
  useLiveWatch([deviceId])
  const frame = useStore((s) => s.live[deviceId])
  const connected = useStore((s) => s.sensors[deviceId]?.live?.link === 'connected')
  return (
    <section className={styles.strip} aria-labelledby="live-strip-title">
      <div className={styles.stripHead}>
        <h2 id="live-strip-title" className={styles.stripTitle}>
          <Activity size={18} aria-hidden />
          {t.live.stripTitle}
        </h2>
        <Link href={`/monitor?ids=${encodeURIComponent(deviceId)}`} className={styles.stripLink}>
          {t.live.openMonitor}
          <ArrowRight size={16} aria-hidden />
        </Link>
      </div>
      {frame ? (
        <>
          {!connected && <p className={styles.note}>{t.live.stale}</p>}
          <PresenceChips frame={frame} />
          <ZoneList zones={frame.zones} compact />
        </>
      ) : (
        <p className={styles.note}>{connected ? t.live.waiting : t.live.stale}</p>
      )}
    </section>
  )
}
