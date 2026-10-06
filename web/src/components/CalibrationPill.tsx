import { Loader2, Timer } from 'lucide-react'
import { Link } from 'wouter'
import { formatCountdown, formatHHMM, selectBatchTally } from '../calibration'
import { useCountdown } from '../hooks/useCountdown'
import { useStore } from '../store/store'
import { useStrings } from '../strings'
import styles from './shell.module.css'

/** In the header of every screen while a batch waits or runs, so a countdown is never out of sight (D11). */
export function CalibrationPill() {
  const t = useStrings()
  const batch = useStore((s) => s.batch)
  const remaining = useCountdown()
  if (batch?.state === 'waiting') {
    const text =
      batch.start === 'at'
        ? t.batch.pillAt(formatHHMM(new Date(batch.fire_at * 1000)))
        : t.batch.pillWaiting(formatCountdown(remaining ?? 0))
    return (
      <Link href="/calibrate" className={`${styles.pill} ${styles.pillWait} ${styles.pillCal}`} aria-label={text}>
        <Timer size={16} aria-hidden />
        <span className={`${styles.pillCount} ${styles.squeeze}`}>{text}</span>
      </Link>
    )
  }
  if (batch?.state === 'running') {
    const { ended, total } = selectBatchTally(batch)
    return (
      <Link
        href="/calibrate"
        className={`${styles.pill} ${styles.pillCal}`}
        aria-label={t.batch.pillRunning(ended, total)}
      >
        <Loader2 size={16} className="spin" aria-hidden />
        <span className={`${styles.pillCount} ${styles.squeeze}`}>{t.batch.pillRunning(ended, total)}</span>
      </Link>
    )
  }
  return null
}
