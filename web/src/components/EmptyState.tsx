import { Plus, Radar } from 'lucide-react'
import { Link } from 'wouter'
import { useStrings } from '../strings'
import { buttonClass } from './Button'
import styles from './dashboard.module.css'

export function EmptyState() {
  const t = useStrings()
  return (
    <div className={styles.empty}>
      <div className={styles.emptyIcon} aria-hidden>
        <Radar size={44} />
      </div>
      <p className={styles.emptyTitle}>{t.dash.empty}</p>
      <p className={styles.emptyBody}>{t.gather.idleBody}</p>
      <Link href="/gather" className={buttonClass('primary')}>
        <Plus size={18} aria-hidden />
        {t.nav.gather}
      </Link>
    </div>
  )
}
