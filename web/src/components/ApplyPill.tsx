import { Loader2 } from 'lucide-react'
import { Link } from 'wouter'
import { selectApplyTally, useStore } from '../store/store'
import { t } from '../strings'
import styles from './shell.module.css'

/** In the header while an apply job runs, on every screen and every client (G25). */
export function ApplyPill() {
  const job = useStore((s) => s.apply)
  if (job?.state !== 'running') return null
  const { ended, total } = selectApplyTally(job)
  const only = job.items.length === 1 ? job.items[0]?.device_id : undefined
  return (
    <Link href={only ? `/sensors/${encodeURIComponent(only)}/settings` : '/bulk'} className={`${styles.pill} ${styles.pillCal}`}>
      <Loader2 size={16} className="spin" aria-hidden />
      <span className={styles.pillCount}>{t.apply.pill(ended, total)}</span>
    </Link>
  )
}
