import { Bluetooth, Square } from 'lucide-react'
import { useState } from 'react'
import { bluetoothUnavailable } from '../api/bluetooth'
import { failureText, startGather, stopGather } from '../api/client'
import { useStore } from '../store/store'
import { useStrings } from '../strings'
import { Button } from './Button'
import styles from './gather.module.css'

/** The gather screen's one primary action: 64 px, docked above the tab bar on phones. */
export function GatherToggle() {
  const t = useStrings()
  const gathering = useStore((s) => s.gather.gathering)
  const server = useStore((s) => s.server)
  const transport = server && (server as typeof server & { ble_transport?: 'browser' | 'server' }).ble_transport
  const browser = server !== null && server.sim === null && (transport ?? 'browser') === 'browser'
  const unavailable = browser ? bluetoothUnavailable() : null
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<unknown>(null)

  const run = async (action: () => Promise<unknown>) => {
    setBusy(true)
    setError(null)
    try {
      await action()
    } catch (e) {
      setError(e)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className={styles.toggleDock}>
      <div className={styles.toggleInner}>
        {unavailable && <p className={styles.formError} role="alert">{unavailable}</p>}
        {error !== null && (
          <p className={styles.formError} role="alert">
            {failureText(error)}
          </p>
        )}
        <div className={styles.toggleActions}>
          {browser && gathering && (
            <Button
              variant="primary"
              size="lg"
              block
              icon={Bluetooth}
              disabled={busy || unavailable !== null}
              onClick={() => run(startGather)}
            >
              {t.gather.add}
            </Button>
          )}
          <Button
            variant={gathering ? 'secondary' : 'primary'}
            size="lg"
            block
            icon={gathering ? Square : Bluetooth}
            className={gathering ? styles.stopping : undefined}
            disabled={busy || server === null || (!gathering && unavailable !== null)}
            onClick={() => run(gathering ? stopGather : startGather)}
          >
            {gathering ? t.gather.stop : t.gather.start}
          </Button>
        </div>
      </div>
    </div>
  )
}
