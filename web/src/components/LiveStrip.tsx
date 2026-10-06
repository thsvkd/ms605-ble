import { Activity, ArrowRight } from 'lucide-react'
import { Link } from 'wouter'
import { useLiveWatch } from '../hooks/useLiveWatch'
import { presenceOf } from '../presence'
import { useStore } from '../store/store'
import { useStrings } from '../strings'
import { PresenceLegend, PresencePanel } from './PresenceSignals'
import { ZoneList } from './ZoneMeterRow'
import styles from './live.module.css'

/**
 * Sensor detail: the device's 재실 call, the raw PIR / RF signals and the trigger bars of one sensor, as the
 * dashboard and monitor draw them (14.8.5.1). Watched only while this page is open and visible.
 */
export function LiveStrip({ deviceId }: { deviceId: string }) {
  const t = useStrings()
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
          <PresencePanel presence={presenceOf(frame)} />
          <ZoneList zones={frame.zones} />
          <PresenceLegend />
        </>
      ) : (
        <p className={styles.note}>{connected ? t.live.waiting : t.live.stale}</p>
      )}
    </section>
  )
}
