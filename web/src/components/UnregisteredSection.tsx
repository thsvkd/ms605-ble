import { Sparkles, Tag } from 'lucide-react'
import { useMemo, useState } from 'react'
import { sensorDisplayName } from '../alias'
import { useNow } from '../hooks/useNow'
import { sensorStatus } from '../status'
import { selectUnregistered, useStore } from '../store/store'
import { useStrings } from '../strings'
import { BatteryIndicator } from './BatteryIndicator'
import { Button } from './Button'
import { NameSensorDialog } from './NameSensorDialog'
import { StatusBadge } from './StatusBadge'
import styles from './dashboard.module.css'

export function UnregisteredSection() {
  const t = useStrings()
  const sensors = useStore((s) => s.sensors)
  const gathering = useStore((s) => s.gather.gathering)
  const list = useMemo(() => selectUnregistered({ sensors }), [sensors])
  const now = useNow()
  const [naming, setNaming] = useState<string | null>(null)
  if (list.length === 0) return null
  return (
    <section className={styles.section} aria-labelledby="unregistered-title">
      <h2 id="unregistered-title" className={styles.sectionHead}>
        {t.dash.unregistered}
        <span className={styles.sectionCount}>({list.length})</span>
      </h2>
      <ul className={`${styles.grid} ${styles.unregisteredGrid}`}>
        {list.map((s) => {
          const status = sensorStatus(s, gathering, now)
          return (
            <li key={s.device_id} className={`${styles.card} ${styles.unregistered}`} data-kind={status.kind}>
              <div className={styles.cardHead}>
                <span className={styles.newBadge}>
                  <Sparkles size={12} aria-hidden />
                  {t.sensor.new}
                </span>
                <span className={styles.bleName}>{sensorDisplayName(s, s.device_id)}</span>
              </div>
              <div className={styles.cardMeta}>
                <StatusBadge status={status} />
                <BatteryIndicator sensor={s} />
              </div>
              {status.hint && (
                <p className={styles.hint} data-kind={status.kind}>
                  {status.hint}
                </p>
              )}
              <div className={styles.cardActions}>
                <Button icon={Tag} onClick={() => setNaming(s.device_id)}>
                  {t.form.nameAction}
                </Button>
              </div>
            </li>
          )
        })}
      </ul>
      <NameSensorDialog deviceId={naming} onClose={() => setNaming(null)} />
    </section>
  )
}
