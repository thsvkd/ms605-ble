import { Check } from 'lucide-react'
import type { SensorView } from '../api/types'
import { sensorName } from '../store/store'
import { useStrings } from '../strings'
import { Button } from './Button'
import styles from './live.module.css'

interface Props {
  sensors: SensorView[]
  selected: readonly string[]
  onChange: (ids: string[]) => void
}

/** Toggle chips for the sensors that have a session; wraps, never scrolls sideways. */
export function SensorPicker({ sensors, selected, onChange }: Props) {
  const t = useStrings()
  const chosen = new Set(selected)
  const toggle = (id: string) => {
    if (chosen.has(id)) onChange(selected.filter((x) => x !== id))
    else onChange([...selected, id])
  }
  return (
    <fieldset className={styles.picker}>
      <legend className={styles.pickerLabel}>{t.monitor.pick}</legend>
      <div className={styles.pickerChips}>
        {sensors.map((s) => {
          const on = chosen.has(s.device_id)
          return (
            <button
              key={s.device_id}
              type="button"
              className={styles.toggle}
              aria-pressed={on}
              onClick={() => toggle(s.device_id)}
            >
              <span className={styles.toggleCheck} aria-hidden>
                {on && <Check size={11} strokeWidth={3} />}
              </span>
              {sensorName(s, s.device_id)}
            </button>
          )
        })}
      </div>
      <div className={styles.pickerActions}>
        <Button variant="ghost" onClick={() => onChange(sensors.map((s) => s.device_id))}>
          {t.monitor.all}
        </Button>
        <Button variant="ghost" onClick={() => onChange([])}>
          {t.monitor.none}
        </Button>
      </div>
    </fieldset>
  )
}
