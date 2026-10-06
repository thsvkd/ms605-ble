import { AlertTriangle, CircleStop, Info, PauseCircle, XCircle } from 'lucide-react'
import { useEffect, useState } from 'react'
import { cancelBatch, failureText } from '../../api/client'
import type { BatchView, SensorView } from '../../api/types'
import { batchHeadline, formatCountdown, formatElapsed, needsCancelConfirm } from '../../calibration'
import { useCountdown } from '../../hooks/useCountdown'
import { useStrings } from '../../strings'
import { Button } from '../Button'
import { ConfirmDialog } from '../ConfirmDialog'
import styles from './calibrate.module.css'
import { JobRow } from './JobRow'

interface Props {
  batch: BatchView
  sensors: Record<string, SensorView>
  gathering: boolean
}

/** Waiting (countdown) or running. Every screen shows the same thing: the batch is server state (G16). */
export function BatchProgress({ batch, sensors, gathering }: Props) {
  const t = useStrings()
  const remaining = useCountdown()
  const [confirm, setConfirm] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<unknown>(null)
  const waiting = batch.state === 'waiting'
  const headline = batchHeadline(batch, remaining, new Date())

  // Waiting with time left: stop at once, nothing has touched a sensor yet. Running, or about to fire
  // (G38: the cancel may land once the round runs): it drops links, so confirm (10.1-5).
  const confirmFirst = needsCancelConfirm(batch, remaining)

  // the round ended (or a new one replaced it) while the dialog was open: nothing left to confirm
  const ended = batch.state !== 'waiting' && batch.state !== 'running'
  useEffect(() => {
    if (ended) setConfirm(false)
  }, [ended])

  const cancelNow = async () => {
    setBusy(true)
    setError(null)
    try {
      await cancelBatch(batch.batch_id)
    } catch (e) {
      setError(e)
    } finally {
      setBusy(false)
    }
  }

  const cancelLabel = !waiting
    ? t.batch.cancelRunning
    : batch.start === 'at'
      ? t.batch.cancelScheduled
      : t.batch.cancelWaiting

  return (
    <div className={styles.layout}>
      <div className={styles.aside}>
        {waiting && batch.start === 'delay' ? (
          <div className={styles.countdown} role="timer" aria-label={headline}>
            <span className={styles.countdownValue} aria-hidden>
              {formatCountdown(remaining ?? Math.max(0, batch.fire_at - Date.now() / 1000))}
            </span>
            <span className={styles.countdownSub} aria-hidden>
              {t.batch.countdownSub}
            </span>
          </div>
        ) : (
          <h2 className={styles.headline}>{headline}</h2>
        )}
        {waiting && batch.start === 'at' && (
          <p className={styles.leaveRoom}>
            <Info size={16} aria-hidden />
            {t.start.atNote}
          </p>
        )}
        {waiting && batch.start !== 'at' && <p className={styles.muted}>{t.preflight.leaveRoom}</p>}
        {batch.presence_override && (
          <span className={`${styles.chip} ${styles.chipWarn} ${styles.chipStart}`}>
            <AlertTriangle size={14} aria-hidden />
            {t.batch.override}
          </span>
        )}
        {!waiting && <p className={styles.small}>{t.batch.progressNote(formatElapsed(batch.expected_s))}</p>}
        {!waiting && !gathering && (
          <p className={`${styles.small} ${styles.iconLine}`}>
            <PauseCircle size={14} aria-hidden />
            {t.batch.gatherPaused}
          </p>
        )}
        <div className={styles.dock}>
          <div className={styles.dockInner}>
            {error !== null && (
              <p className={styles.error} role="alert">
                {failureText(error)}
              </p>
            )}
            <Button
              variant="danger"
              size="lg"
              block
              icon={waiting ? XCircle : CircleStop}
              disabled={busy}
              onClick={confirmFirst ? () => setConfirm(true) : cancelNow}
            >
              {cancelLabel}
            </Button>
          </div>
        </div>
      </div>
      <div className={styles.body}>
        <ul className={styles.rows} aria-label={t.batch.sensors}>
          {batch.jobs.map((job) => (
            <JobRow key={job.device_id} job={job} batch={batch} sensor={sensors[job.device_id]} gathering={gathering} />
          ))}
        </ul>
      </div>
      <ConfirmDialog
        open={confirm}
        title={cancelLabel}
        body={waiting ? t.batch.cancelConfirmSoon : t.batch.cancelConfirm}
        confirmLabel={cancelLabel}
        danger
        onConfirm={() => cancelBatch(batch.batch_id)}
        onClose={() => setConfirm(false)}
      />
    </div>
  )
}
