import { Loader2 } from 'lucide-react'
import { useStore } from '../store/store'
import { useStrings } from '../strings'
import styles from './gather.module.css'

export function ConnectingLine() {
  const t = useStrings()
  const n = useStore((s) => s.gather.connecting.length)
  if (n === 0) return null
  return (
    <p className={styles.connecting}>
      <Loader2 size={16} className="spin" aria-hidden />
      {t.gather.connecting(n)}
    </p>
  )
}
