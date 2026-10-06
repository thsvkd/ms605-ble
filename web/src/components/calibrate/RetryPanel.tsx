import { Bluetooth, RotateCcw } from 'lucide-react'
import { useState } from 'react'
import { failureText, retryBatch, startGather } from '../../api/client'
import type { BatchView, SensorView } from '../../api/types'
import { sensorName } from '../../store/store'
import { useStrings } from '../../strings'
import { Button } from '../Button'
import styles from './calibrate.module.css'
import { type StartChoice, StartOptions, startBody, startLabel } from './StartOptions'

interface Props {
  batch: BatchView
  sensors: Record<string, SensorView>
  gathering: boolean
  choice: StartChoice
  onChoice: (c: StartChoice) => void
}

const RETRY_MODES = ['now', 'delay'] as const

/** Only the failed or lost sensors, once they are connected again. No new preflight (14.5.4). */
export function RetryPanel({ batch, sensors, gathering, choice, onChoice }: Props) {
  const t = useStrings()
  // remember what the operator unticked, so a sensor that reconnects later arrives ticked
  const [unticked, setUnticked] = useState<ReadonlySet<string>>(new Set())
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<unknown>(null)
  const candidates = batch.jobs.filter((j) => j.retryable)
  if (candidates.length === 0) return null

  const connected = (id: string) => sensors[id]?.live?.link === 'connected'
  const picked = candidates.map((j) => j.device_id).filter((id) => connected(id) && !unticked.has(id))
  const anyAway = candidates.some((j) => !connected(j.device_id))
  const now = new Date()
  const body = startBody(choice, now)

  const toggle = (id: string) => {
    const next = new Set(unticked)
    if (next.has(id)) next.delete(id)
    else next.add(id)
    setUnticked(next)
  }

  const run = async (fn: () => Promise<unknown>) => {
    setBusy(true)
    setError(null)
    try {
      await fn()
    } catch (e) {
      setError(e)
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <section className={styles.panel} aria-labelledby="retry-title">
        <h2 id="retry-title" className={styles.panelTitle}>
          {t.retry.title(candidates.length)}
        </h2>
        <ul className={styles.rows}>
          {candidates.map((j) => {
            const ok = connected(j.device_id)
            return (
              <li key={j.device_id} className={styles.row} data-kind={ok ? undefined : 'warn'}>
                <label className={styles.check} data-disabled={!ok}>
                  <input
                    type="checkbox"
                    disabled={!ok}
                    checked={ok && !unticked.has(j.device_id)}
                    onChange={() => toggle(j.device_id)}
                  />
                  <span className={styles.rowName}>{sensorName(sensors[j.device_id], j.device_id)}</span>
                </label>
                <p className={styles.rowHint} data-kind={ok ? undefined : 'warn'}>
                  {ok ? t.retry.reconnected : gathering ? t.retry.pressAgain : sensors[j.device_id]?.live?.auto_reconnect ? t.link.reconnectHint : t.retry.pressAgainGather}
                </p>
              </li>
            )
          })}
        </ul>
        {anyAway && !gathering && candidates.some((j) => !connected(j.device_id) && !sensors[j.device_id]?.live?.auto_reconnect) && (
          <Button icon={Bluetooth} disabled={busy} onClick={() => run(startGather)}>
            {t.retry.startGather}
          </Button>
        )}
        <h3 className={styles.panelTitle}>{t.start.title}</h3>
        <StartOptions value={choice} onChange={onChoice} modes={RETRY_MODES} now={now} />
        {choice.mode === 'delay' && <p className={styles.muted}>{t.preflight.leaveRoom}</p>}
      </section>
      <div className={styles.dock}>
        <div className={styles.dockInner}>
        {error !== null && (
            <p className={styles.error} role="alert">
              {failureText(error)}
            </p>
          )}
          <Button
            variant="primary"
            size="lg"
            block
            icon={RotateCcw}
            disabled={busy || picked.length === 0 || body === null}
            onClick={() => body && run(() => retryBatch(batch.batch_id, { device_ids: picked, ...body }))}
            aria-describedby="retry-start"
          >
            {t.retry.go(picked.length)}
          </Button>
          <span id="retry-start" className="visually-hidden">
            {startLabel(choice, now)}
          </span>
        </div>
      </div>
    </>
  )
}
