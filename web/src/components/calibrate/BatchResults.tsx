import { ChevronRight, Plus } from 'lucide-react'
import type { BatchView, SensorView } from '../../api/types'
import { batchHeadline } from '../../calibration'
import { sensorName } from '../../store/store'
import { useStrings } from '../../strings'
import { Button } from '../Button'
import styles from './calibrate.module.css'
import { JobRow } from './JobRow'
import { RetryPanel } from './RetryPanel'
import type { StartChoice } from './StartOptions'
import { ThresholdCompare } from './ThresholdCompare'

interface Props {
  batch: BatchView
  sensors: Record<string, SensorView>
  gathering: boolean
  retryChoice: StartChoice
  onRetryChoice: (c: StartChoice) => void
  onNew: () => void
}

/** Done or cancelled: per-sensor outcome, before/after thresholds, and a retry for the failed or lost. */
export function BatchResults({ batch, sensors, gathering, retryChoice, onRetryChoice, onNew }: Props) {
  const t = useStrings()
  const retry = batch.jobs.some((j) => j.retryable)
  return (
    <>
      <h2 className={styles.headline} role="status">
        {batchHeadline(batch, null, new Date())}
      </h2>
      <div className={`${styles.layout} ${styles.bodyFirst}`}>
        <div className={styles.aside}>
          {retry && (
            <RetryPanel
              batch={batch}
              sensors={sensors}
              gathering={gathering}
              choice={retryChoice}
              onChoice={onRetryChoice}
            />
          )}
          {retry ? (
            <Button icon={Plus} onClick={onNew}>
              {t.batch.newBatch}
            </Button>
          ) : (
            <div className={styles.dock}>
              <div className={styles.dockInner}>
                <Button variant="primary" size="lg" block icon={Plus} onClick={onNew}>
                  {t.batch.newBatch}
                </Button>
              </div>
            </div>
          )}
        </div>
        <div className={styles.body}>
          <ul className={styles.rows} aria-label={t.batch.sensors}>
            {batch.jobs.map((job) => {
              const alias = sensorName(sensors[job.device_id], job.device_id)
              return (
                <JobRow
                  key={job.device_id}
                  job={job}
                  batch={batch}
                  sensor={sensors[job.device_id]}
                  gathering={gathering}
                >
                  {job.state === 'succeeded' && (
                    <details className={styles.details}>
                      <summary>
                        <ChevronRight size={16} className={styles.chevron} aria-hidden />
                        {t.compare.show}
                      </summary>
                      <ThresholdCompare job={job} alias={alias} />
                    </details>
                  )}
                </JobRow>
              )
            })}
          </ul>
        </div>
      </div>
    </>
  )
}
