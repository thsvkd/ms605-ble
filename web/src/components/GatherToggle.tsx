import { Bluetooth, Square } from 'lucide-react'
import { useState } from 'react'
import { ApiRequestError, startGather, stopGather } from '../api/client'
import { useStore } from '../store/store'
import { errorText, t } from '../strings'
import { Button } from './Button'
import styles from './gather.module.css'

/** The gather screen's one primary action: 64 px, docked above the tab bar on phones. */
export function GatherToggle() {
  const gathering = useStore((s) => s.gather.gathering)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const toggle = async () => {
    setBusy(true)
    setError(null)
    try {
      await (gathering ? stopGather() : startGather())
    } catch (e) {
      setError(e instanceof ApiRequestError ? errorText(e.code, e.message) : t.error.internal)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className={styles.toggleDock}>
      <div className={styles.toggleInner}>
        {error && (
          <p className={styles.formError} role="alert">
            {error}
          </p>
        )}
        <Button
          variant={gathering ? 'secondary' : 'primary'}
          size="lg"
          block
          icon={gathering ? Square : Bluetooth}
          className={gathering ? styles.stopping : undefined}
          disabled={busy}
          onClick={toggle}
        >
          {gathering ? t.gather.stop : t.gather.start}
        </Button>
      </div>
    </div>
  )
}
