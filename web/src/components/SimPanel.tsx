import { ChevronRight, Hand, Unplug } from 'lucide-react'
import { useState } from 'react'
import { failureText, simDrop, simPress, simPressAll } from '../api/client'
import { useStore } from '../store/store'
import { useStrings } from '../strings'
import { Button } from './Button'
import styles from './gather.module.css'

/** Only with --sim: stands in for a person walking around pressing buttons (6.6). */
export function SimPanel() {
  const t = useStrings()
  const count = useStore((s) => s.server?.sim?.count ?? 0)
  const [error, setError] = useState<unknown>(null)
  if (count === 0) return null

  const run = (fn: () => Promise<void>) => async () => {
    setError(null)
    try {
      await fn()
    } catch (e) {
      setError(e)
    }
  }
  const indexes = Array.from({ length: count }, (_, i) => i + 1)

  return (
    <details className={styles.details}>
      <summary className={styles.summary}>
        <ChevronRight size={18} className={styles.chevron} aria-hidden />
        {t.sim.title}
      </summary>
      <div className={styles.detailsBody}>
        <Button variant="secondary" icon={Hand} onClick={run(simPressAll)}>
          {t.sim.pressAll}
        </Button>
        <div className={styles.simGrid}>
          {indexes.map((n) => (
            <Button key={`p${n}`} onClick={run(() => simPress(n))}>
              {t.sim.press(n)}
            </Button>
          ))}
        </div>
        <div className={styles.simGrid}>
          {indexes.map((n) => (
            <Button key={`d${n}`} variant="ghost" icon={Unplug} onClick={run(() => simDrop(n))}>
              {t.sim.drop(n)}
            </Button>
          ))}
        </div>
        {error !== null && (
          <p className={`${styles.result} ${styles.resultError}`} role="alert">
            {failureText(error)}
          </p>
        )}
      </div>
    </details>
  )
}
