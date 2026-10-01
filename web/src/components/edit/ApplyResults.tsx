import { Plus, Undo2, X } from 'lucide-react'
import { useState } from 'react'
import type { ApplyJobView, RollbackItem } from '../../api/types'
import { applyHeadline, applyItemStatus, canRollback, formatDateTime, snapshotTime } from '../../apply'
import { sensorName, useStore } from '../../store/store'
import { t } from '../../strings'
import { Button } from '../Button'
import cal from '../calibrate/calibrate.module.css'
import { Dialog } from '../Dialog'
import { StatusBadge } from '../StatusBadge'
import styles from './edit.module.css'
import { RollbackPreview } from './RollbackPicker'

interface Props {
  job: ApplyJobView
  /** One sensor only (its settings tab). */
  only?: string
  onDismiss: () => void
  /** 닫기 on a sensor, 새 초안 on the bulk screen. */
  dismissLabel?: string
  /** A rollback started from here (202). */
  onStarted?: (job: ApplyJobView) => void
}

/**
 * Per-sensor outcome of an apply / rollback / clone job (15.9.12): verified ok, partial · unverified ·
 * not started warn, failed while writing danger. No retry (G37): what reached the write can roll back.
 */
export function ApplyResults({ job, only, onDismiss, dismissLabel = t.apply.close, onStarted }: Props) {
  const sensors = useStore((s) => s.sensors)
  const [rb, setRb] = useState<RollbackItem[] | null>(null)
  const items = only ? job.items.filter((i) => i.device_id === only) : job.items
  const view = { ...job, items }
  const done = job.state === 'done'
  const back = done ? items.filter(canRollback) : []
  const name = (id: string) => sensorName(sensors[id], id)

  const title =
    rb?.length === 1 && rb[0] ? t.rollback.title(formatDateTime(snapshotTime(rb[0].snapshot))) : t.apply.rollbackAll

  return (
    <section className={styles.results} aria-label={applyHeadline(view)}>
      {/* LiveRegion announces start and end (15.9.12); the heading stays a heading */}
      <h2 className={styles.resultsHead}>{applyHeadline(view)}</h2>
      <ul className={cal.rows}>
        {items.map((item) => {
          const status = applyItemStatus(item, job.kind)
          return (
            <li key={item.device_id} className={cal.row} data-kind={status.kind}>
              <div className={cal.rowHead}>
                <span className={cal.rowName}>{name(item.device_id)}</span>
                <span className={cal.rowEnd}>
                  <StatusBadge status={status} />
                </span>
              </div>
              {status.hint && (
                <p className={cal.rowHint} data-kind={status.kind}>
                  {status.hint}
                </p>
              )}
              {done && canRollback(item) && item.snapshot && (
                <div className={styles.rowActions}>
                  <Button
                    icon={Undo2}
                    aria-label={`${name(item.device_id)} ${t.apply.rollback}`}
                    onClick={() => item.snapshot && setRb([{ device_id: item.device_id, snapshot: item.snapshot }])}
                  >
                    {t.apply.rollback}
                  </Button>
                </div>
              )}
            </li>
          )
        })}
      </ul>
      {done && (
        <div className={styles.rowActions}>
          {back.length >= 2 && (
            <Button
              icon={Undo2}
              onClick={() =>
                setRb(back.flatMap((i) => (i.snapshot ? [{ device_id: i.device_id, snapshot: i.snapshot }] : [])))
              }
            >
              {t.apply.rollbackAll}
            </Button>
          )}
          <Button icon={dismissLabel === t.apply.close ? X : Plus} onClick={onDismiss}>
            {dismissLabel}
          </Button>
        </div>
      )}
      <Dialog open={rb !== null} title={title} onClose={() => setRb(null)} className={styles.wideDialog}>
        {rb && (
          <RollbackPreview
            items={rb}
            title={title}
            sheet
            onBack={() => setRb(null)}
            onStarted={(j) => {
              setRb(null)
              onStarted?.(j)
            }}
          />
        )}
      </Dialog>
    </section>
  )
}
