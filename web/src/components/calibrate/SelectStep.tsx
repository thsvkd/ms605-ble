import { ArrowRight } from 'lucide-react'
import type { SensorView } from '../../api/types'
import { useNow } from '../../hooks/useNow'
import { busyText, sensorStatus } from '../../status'
import { sensorName } from '../../store/store'
import { t } from '../../strings'
import { Button } from '../Button'
import { StatusBadge } from '../StatusBadge'
import styles from './calibrate.module.css'

/** Connected and not held by another operation ("identify" is a momentary read the core waits out, G22). */
export function selectable(s: SensorView): boolean {
  return s.live?.link === 'connected' && (s.live.busy === null || s.live.busy === 'identify')
}

function reason(s: SensorView): string | null {
  if (s.live?.link !== 'connected') return t.calib.notConnected
  if (s.live.busy && s.live.busy !== 'identify') return t.busy.label(busyText(s.live.busy))
  return null
}

interface Props {
  sessions: SensorView[]
  checked: ReadonlySet<string>
  gathering: boolean
  onToggle: (id: string) => void
  onSelectAll: () => void
  onNext: () => void
}

export function SelectStep({ sessions, checked, gathering, onToggle, onSelectAll, onNext }: Props) {
  const now = useNow()
  const count = sessions.filter((s) => selectable(s) && checked.has(s.device_id)).length
  return (
    <div className={styles.layout}>
      <div className={styles.aside}>
        <div className={styles.panel}>
          <h2 className={styles.headline}>{t.calib.selectTitle}</h2>
          <p className={styles.muted}>{t.calib.selectHint}</p>
          <Button onClick={onSelectAll} disabled={!sessions.some(selectable)}>
            {t.calib.selectAll}
          </Button>
        </div>
        <div className={styles.dock}>
          <div className={styles.dockInner}>
            <Button variant="primary" size="lg" block icon={ArrowRight} disabled={count === 0} onClick={onNext}>
              {t.calib.next(count)}
            </Button>
          </div>
        </div>
      </div>
      <div className={styles.body}>
        {sessions.length === 0 ? (
          <p className={styles.panel}>{t.calib.noSensors}</p>
        ) : (
          <ul className={styles.rows}>
            {sessions.map((s) => {
              const ok = selectable(s)
              const why = reason(s)
              return (
                <li key={s.device_id} className={styles.row}>
                  <label className={styles.check} data-disabled={!ok}>
                    <input
                      type="checkbox"
                      checked={ok && checked.has(s.device_id)}
                      disabled={!ok}
                      onChange={() => onToggle(s.device_id)}
                    />
                    <span className={styles.rowName}>{sensorName(s, s.device_id)}</span>
                    {s.registry?.location && <span className={styles.rowSub}>{s.registry.location}</span>}
                    <span className={styles.rowEnd}>
                      <StatusBadge status={sensorStatus(s, gathering, now)} />
                    </span>
                  </label>
                  {why && <p className={styles.rowHint}>{why}</p>}
                </li>
              )
            })}
          </ul>
        )}
      </div>
    </div>
  )
}
