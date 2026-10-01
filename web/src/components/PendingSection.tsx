import { useStore } from '../store/store'
import { t } from '../strings'
import styles from './dashboard.module.css'

/** Imported entries whose sensor has not connected yet. */
export function PendingSection() {
  const pending = useStore((s) => s.pending)
  const sites = useStore((s) => s.sites)
  if (pending.length === 0) return null
  return (
    <section className={styles.section} aria-labelledby="pending-title">
      <h2 id="pending-title" className={styles.sectionHead}>
        {t.dash.pending}
        <span className={styles.sectionCount}>({pending.length})</span>
      </h2>
      <ul className={styles.pendingList}>
        {pending.map((p) => (
          <li key={`${p.site_id}/${p.address}`} className={styles.pendingItem}>
            <span className={styles.pendingAlias}>{p.alias}</span>
            <span className={styles.pendingSite}>· {sites[p.site_id]?.name ?? p.site_id}</span>
            <span className={styles.pendingHint}>{t.dash.pendingHint}</span>
          </li>
        ))}
      </ul>
    </section>
  )
}
