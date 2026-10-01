import { Loader2, WifiOff } from 'lucide-react'
import { useStore } from '../store/store'
import { t } from '../strings'
import styles from './shell.module.css'

/** Shown while the WS is not open; after two failed reconnects it says the server is unreachable. */
export function ConnectionBanner() {
  const conn = useStore((s) => s.conn)
  const failures = useStore((s) => s.failures)
  if (conn === 'open' || conn === 'unauthorized') return null
  if (conn === 'connecting' && failures === 0) return null // first load: nothing to warn about yet
  const down = failures >= 2
  return (
    <div className={`${styles.banner} ${down ? styles.bannerDown : ''}`} role="status">
      {down ? <WifiOff size={16} aria-hidden /> : <Loader2 size={16} className="spin" aria-hidden />}
      {down ? t.conn.down : t.conn.lost}
    </div>
  )
}
