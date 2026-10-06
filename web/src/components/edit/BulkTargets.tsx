import { CheckCheck } from 'lucide-react'
import type { SensorView } from '../../api/types'
import { useNow } from '../../hooks/useNow'
import { sensorStatus } from '../../status'
import { sensorName } from '../../store/store'
import { t, useStrings } from '../../strings'
import { Button } from '../Button'
import cal from '../calibrate/calibrate.module.css'
import { StatusBadge } from '../StatusBadge'
import styles from './edit.module.css'

interface Props {
  sessions: SensorView[]
  selected: readonly string[]
  batchLocked: ReadonlySet<string>
  applyLocked: ReadonlySet<string>
  gathering: boolean
  onChange: (ids: string[]) => void
  /** The clone source is never a target. */
  exclude?: string | null
}

export function targetReason(
  s: SensorView,
  batchLocked: ReadonlySet<string>,
  applyLocked: ReadonlySet<string>,
): string | null {
  if (s.live?.link !== 'connected') return t.calib.notConnected
  if (batchLocked.has(s.device_id)) return t.bulk.lockedBatch
  if (applyLocked.has(s.device_id)) return t.apply.locked
  return null
}

/** Sensors with a session (site, then alias); only connected ones outside a calibration or apply can be ticked. */
export function BulkTargets({ sessions, selected, batchLocked, applyLocked, gathering, onChange, exclude }: Props) {
  const t = useStrings()
  const now = useNow()
  const list = sessions.filter((s) => s.device_id !== exclude)
  const ok = list.filter((s) => targetReason(s, batchLocked, applyLocked) === null).map((s) => s.device_id)
  const picked = selected.filter((id) => ok.includes(id))
  const toggle = (id: string) =>
    onChange(selected.includes(id) ? selected.filter((x) => x !== id) : list.map((s) => s.device_id).filter((x) => x === id || selected.includes(x)))
  return (
    <section className={styles.section} aria-label={t.bulk.targets}>
      <div className={styles.sectionHead}>
        <h2 className={styles.sectionTitle}>
          {t.bulk.targets} · {t.bulk.selected(picked.length)}
        </h2>
        <Button icon={CheckCheck} onClick={() => onChange(ok)} disabled={ok.length === 0}>
          {t.bulk.selectAll}
        </Button>
      </div>
      {list.length === 0 ? (
        <p className={styles.note}>{t.bulk.noSessions}</p>
      ) : (
        <ul className={cal.rows}>
          {list.map((s) => {
            const why = targetReason(s, batchLocked, applyLocked)
            return (
              <li key={s.device_id} className={cal.row}>
                <label className={cal.check} data-disabled={why !== null}>
                  <input
                    type="checkbox"
                    checked={why === null && selected.includes(s.device_id)}
                    disabled={why !== null}
                    onChange={() => toggle(s.device_id)}
                  />
                  <span className={cal.rowName}>{sensorName(s, s.device_id)}</span>
                  {s.registry?.location && <span className={cal.rowSub}>{s.registry.location}</span>}
                  <span className={cal.rowEnd}>
                    <StatusBadge status={sensorStatus(s, gathering, now)} />
                  </span>
                </label>
                {why && <p className={cal.rowHint}>{why}</p>}
              </li>
            )
          })}
        </ul>
      )}
    </section>
  )
}
