import type { ReactNode } from 'react'
import type { BatchView, CalibrationJobView, SensorView } from '../../api/types'
import { formatElapsed, jobProgress, jobStatus } from '../../calibration'
import { sensorName } from '../../store/store'
import { useStrings } from '../../strings'
import { StatusBadge } from '../StatusBadge'
import styles from './calibrate.module.css'

interface Props {
  job: CalibrationJobView
  batch: BatchView
  sensor: SensorView | undefined
  gathering: boolean
  children?: ReactNode
}

/** One sensor of the batch: its state in words, and while learning, elapsed vs the expected time (G21). */
export function JobRow({ job, batch, sensor, gathering, children }: Props) {
  const t = useStrings()
  const status = jobStatus(job, { batchState: batch.state, gathering, link: sensor?.live?.link ?? null })
  const inRound = batch.round_ids.includes(job.device_id)
  const { ratio, overdue } = jobProgress(job, batch.expected_s)
  const showBar = job.state === 'learning' && ratio !== null
  const pct = Math.round((ratio ?? 0) * 100)

  return (
    <li className={styles.row} data-kind={status.kind} data-dim={!inRound && batch.round > 1}>
      <div className={styles.rowHead}>
        <span className={styles.rowName}>{sensorName(sensor, job.device_id)}</span>
        {job.attempt > 1 && <span className={styles.chip}>{t.batch.attempt(job.attempt)}</span>}
        <span className={styles.rowEnd}>
          <StatusBadge status={status} />
        </span>
      </div>
      {status.hint && (
        <p className={styles.rowHint} data-kind={status.kind}>
          {status.hint}
        </p>
      )}
      {showBar && (
        <>
          <div
            className={styles.bar}
            role="progressbar"
            aria-label={sensorName(sensor, job.device_id)}
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={pct}
            aria-valuetext={t.batch.progressAria(pct)}
          >
            <div className={styles.barFill} style={{ width: `${pct}%` }} />
          </div>
          <p className={styles.barMeta}>
            <span>{t.batch.elapsed(formatElapsed(job.elapsed_s ?? 0), formatElapsed(batch.expected_s))}</span>
            {overdue && <span className={styles.overdue}>{t.batch.overdue}</span>}
          </p>
        </>
      )}
      {children}
    </li>
  )
}
