import { FlaskConical, Wifi } from 'lucide-react'
import { useStore } from '../store/store'
import { t } from '../strings'
import styles from './shell.module.css'

export function LanBadge() {
  const lan = useStore((s) => s.server?.lan ?? false)
  const sim = useStore((s) => s.server?.sim != null)
  return (
    <>
      {lan && (
        <span className={`${styles.chip} ${styles.chipLan}`}>
          <Wifi size={12} aria-hidden />
          <span className={styles.squeeze}>{t.badge.lan}</span>
        </span>
      )}
      {sim && (
        <span className={styles.chip}>
          <FlaskConical size={12} aria-hidden />
          <span className={styles.squeeze}>{t.badge.sim}</span>
        </span>
      )}
    </>
  )
}
