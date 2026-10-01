import { Loader2 } from 'lucide-react'
import { Link } from 'wouter'
import { useStore } from '../store/store'
import { t } from '../strings'
import styles from './shell.module.css'

/** Visible on every screen while gathering, so the state is never hidden (10.1-2). */
export function GatherPill() {
  const gathering = useStore((s) => s.gather.gathering)
  const connecting = useStore((s) => s.gather.connecting.length)
  if (!gathering) return null
  return (
    <Link href="/gather" className={styles.pill}>
      <Loader2 size={16} className="spin" aria-hidden />
      <span className={styles.squeeze}>{t.gather.pill}</span>
      {connecting > 0 && (
        <span className={styles.pillCount} aria-label={t.gather.connecting(connecting)}>
          {connecting}
        </span>
      )}
    </Link>
  )
}
