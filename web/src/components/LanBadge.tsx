import { FlaskConical, Wifi } from 'lucide-react'
import { useStore } from '../store/store'
import { useStrings } from '../strings'
import styles from './shell.module.css'

export function LanBadge() {
  const t = useStrings()
  const lan = useStore((s) => s.server?.lan ?? false)
  const sim = useStore((s) => s.server?.sim != null)
  return (
    <>
      {lan && (
        <span className={`${styles.chip} ${styles.chipLan}`} aria-label={t.badge.lan} title={t.badge.lan}>
          <Wifi size={12} aria-hidden />
          <span className={styles.squeeze}>{t.badge.lan}</span>
        </span>
      )}
      {sim && (
        <span className={styles.chip} aria-label={t.badge.sim} title={t.badge.sim}>
          <FlaskConical size={12} aria-hidden />
          <span className={styles.squeeze}>{t.badge.sim}</span>
        </span>
      )}
    </>
  )
}
