import { ArrowLeft, Info, Loader2, Play, RefreshCw, UserRound } from 'lucide-react'
import { useCallback, useEffect, useRef, useState } from 'react'
import { createBatch, failureText, preflight } from '../../api/client'
import type { PreflightResult, SensorView } from '../../api/types'
import { presenceStatus } from '../../calibration'
import { useNow } from '../../hooks/useNow'
import { sensorName } from '../../store/store'
import { useStrings } from '../../strings'
import { Button } from '../Button'
import { StatusBadge } from '../StatusBadge'
import styles from './calibrate.module.css'
import { type StartChoice, StartOptions, startBody, startLabel } from './StartOptions'

const WINDOW_S = 3 // calibration.PREFLIGHT_WINDOW_S, the server default

interface Props {
  ids: string[]
  sensors: Record<string, SensorView>
  choice: StartChoice
  onChoice: (c: StartChoice) => void
  onBack: () => void
}

type Check = { phase: 'running' } | { phase: 'done'; result: PreflightResult } | { phase: 'error'; error: unknown }

/** Step 2: who is still in the room, then how to start. A warning, never a gate: the operator decides. */
export function PreflightStep({ ids, sensors, choice, onChoice, onBack }: Props) {
  const t = useStrings()
  const [check, setCheck] = useState<Check>({ phase: 'running' })
  const [override, setOverride] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<unknown>(null)
  const run = useRef(0)

  const recheck = useCallback(async () => {
    const mine = ++run.current
    setCheck({ phase: 'running' })
    setOverride(false)
    try {
      const result = await preflight(ids)
      if (run.current === mine) setCheck({ phase: 'done', result })
    } catch (e) {
      if (run.current === mine) setCheck({ phase: 'error', error: e })
    }
  }, [ids])

  useEffect(() => {
    void recheck()
    return () => {
      run.current += 1 // a late answer must not land on an unmounted step
    }
  }, [recheck])

  const results = check.phase === 'done' ? check.result.results : []
  const occupied = results.filter((r) => r.occupied === true)
  const now = new Date(useNow()) // re-render every 30 s so an "at" label never goes stale
  const body = startBody(choice, now)
  const needsOverride = occupied.length > 0 && choice.mode === 'now'
  const canStart = body !== null && !submitting && check.phase !== 'running' && (!needsOverride || override)

  const start = async () => {
    const body = startBody(choice, new Date()) // resolve "at" against the click time, not the last render
    if (!body) return
    setSubmitting(true)
    setError(null)
    try {
      await createBatch({ device_ids: ids, ...body, presence_override: needsOverride && override })
      // the screen moves on when the server's `batch` message arrives (G10)
    } catch (e) {
      setError(e)
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className={`${styles.layout} ${styles.bodyFirst}`}>
      <div className={styles.aside}>
        <section className={styles.panel} aria-labelledby="start-title">
          <h2 id="start-title" className={styles.panelTitle}>
            {t.start.title}
          </h2>
          <StartOptions value={choice} onChange={onChoice} now={now} />
          {needsOverride && (
            <label className={styles.override}>
              <input type="checkbox" checked={override} onChange={(e) => setOverride(e.target.checked)} />
              <span>{t.preflight.override}</span>
            </label>
          )}
          {choice.mode !== 'now' && (
            <p className={styles.leaveRoom}>
              <Info size={16} aria-hidden />
              {t.preflight.leaveRoom}
            </p>
          )}
        </section>
        <div className={styles.dock}>
          <div className={styles.dockInner}>
            {error !== null && (
              <p className={styles.error} role="alert">
                {failureText(error)}
              </p>
            )}
            <Button variant="primary" size="lg" block icon={Play} disabled={!canStart} onClick={start}>
              {startLabel(choice, now)}
            </Button>
          </div>
        </div>
      </div>

      <div className={styles.body}>
        <button type="button" className={styles.back} onClick={onBack}>
          <ArrowLeft size={16} aria-hidden />
          {t.calib.back}
        </button>
        {occupied.length > 0 && (
          <div className={styles.warning} role="alert">
            <span className={styles.warningIcon} aria-hidden>
              <UserRound size={24} />
            </span>
            <p className={styles.warningTitle}>{t.preflight.warnTitle(occupied.length)}</p>
            <p className={styles.warningBody}>{t.preflight.warnBody}</p>
          </div>
        )}
        <section className={styles.panel} aria-labelledby="preflight-title" aria-busy={check.phase === 'running'}>
          <div className={styles.panelHead}>
            <h2 id="preflight-title" className={styles.panelTitle}>
              {t.preflight.title}
            </h2>
            <Button variant="ghost" icon={RefreshCw} disabled={check.phase === 'running'} onClick={recheck}>
              {t.preflight.recheck}
            </Button>
          </div>
          {check.phase === 'running' && (
            <p className={styles.checking} role="status">
              <Loader2 size={20} className="spin" aria-hidden />
              {t.preflight.running(WINDOW_S)}
            </p>
          )}
          {check.phase === 'error' && (
            <p className={styles.error} role="alert">
              {failureText(check.error)}
            </p>
          )}
          {check.phase === 'done' && (
            <ul className={styles.rows}>
              {results.map((r) => {
                const status = presenceStatus(r)
                return (
                  <li key={r.device_id} className={styles.row} data-kind={status.kind}>
                    <div className={styles.rowHead}>
                      <span className={styles.rowName}>{sensorName(sensors[r.device_id], r.device_id)}</span>
                      <span className={styles.rowEnd}>
                        <StatusBadge status={status} />
                      </span>
                    </div>
                    {status.hint && (
                      <p className={styles.rowHint} data-kind={status.kind}>
                        {status.hint}
                      </p>
                    )}
                  </li>
                )
              })}
            </ul>
          )}
        </section>
      </div>
    </div>
  )
}
