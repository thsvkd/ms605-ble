import { AlertTriangle, CheckCircle2, Clock } from 'lucide-react'
import { useMemo } from 'react'
import { selectCounts, useStore } from '../store/store'
import { useStrings } from '../strings'
import styles from './dashboard.module.css'

export function SummaryBar() {
  const t = useStrings()
  const sensors = useStore((s) => s.sensors)
  const c = useMemo(() => selectCounts({ sensors }), [sensors])
  return (
    <ul className={styles.summary} aria-label={t.dash.summaryLabel}>
      <li className={`${styles.stat} ${styles.statOk}`}>
        <CheckCircle2 size={16} aria-hidden />
        {t.dash.connectedN(c.connected)}
      </li>
      {c.lost > 0 && (
        <li className={`${styles.stat} ${styles.statWarn}`}>
          <AlertTriangle size={16} aria-hidden />
          {t.dash.lostN(c.lost)}
        </li>
      )}
      <li className={styles.stat}>
        <Clock size={16} aria-hidden />
        {t.dash.offlineN(c.offline)}
      </li>
      <li className={`${styles.stat} ${styles.statTotal}`}>{t.dash.totalN(c.total)}</li>
    </ul>
  )
}
