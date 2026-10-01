import { AlertTriangle } from 'lucide-react'
import { useEffect, useId, useState } from 'react'
import { t } from '../../strings'
import styles from './edit.module.css'
import { Switch } from './Switch'

const U16_MAX = 65535
const ZONE_INDEXES = [0, 1, 2, 3, 4, 5, 6] as const

function Seconds({
  label,
  value,
  base,
  disabled,
  onChange,
}: {
  label: string
  value: number
  base: number
  disabled: boolean
  onChange: (v: number) => void
}) {
  const id = useId()
  const [text, setText] = useState(String(value))
  useEffect(() => setText(String(value)), [value])
  const n = Number(text)
  const valid = text.trim() !== '' && Number.isInteger(n) && n >= 0 && n <= U16_MAX
  return (
    <div className={styles.fieldRow}>
      <label htmlFor={id}>{label}</label>
      <input
        id={id}
        className={styles.numInput}
        type="number"
        inputMode="numeric"
        min={0}
        max={U16_MAX}
        step={1}
        value={text}
        disabled={disabled}
        aria-invalid={!valid}
        data-changed={value !== base}
        onChange={(e) => {
          setText(e.target.value)
          const v = Number(e.target.value)
          if (e.target.value.trim() !== '' && Number.isInteger(v) && v >= 0 && v <= U16_MAX) onChange(v)
        }}
        onBlur={() => setText(String(value))}
      />
      <span>{t.adv.seconds}</span>
    </div>
  )
}

interface Props {
  index: number
  enabled: boolean
  enabledBase: boolean
  zones: number[]
  timing: [number, number]
  timingBase: [number, number]
  disabled: boolean
  onEnabled: (v: boolean) => void
  /** Gets the latest zones (not this render's), so quick taps never drop one. */
  onZones: (f: (zones: number[]) => number[]) => void
  onTiming: (timing: [number, number]) => void
}

/** One sub-sensor: use (tag41), zones Z0-Z6 (tag48), presence / absence seconds (tag49). */
export function SubSensorEditor(p: Props) {
  const title = useId()
  const name = t.adv.sub(p.index + 1)
  const toggle = (z: number) =>
    p.onZones((zones) => (zones.includes(z) ? zones.filter((x) => x !== z) : [...zones, z].sort((a, b) => a - b)))
  return (
    <section className={styles.section} aria-labelledby={title}>
      <div className={styles.sectionHead}>
        <h3 id={title} className={styles.sectionTitle}>
          {name}
        </h3>
        <Switch
          checked={p.enabled}
          changed={p.enabled !== p.enabledBase}
          label={`${name} ${t.adv.use}`}
          text={t.adv.use}
          disabled={p.disabled}
          onChange={p.onEnabled}
        />
      </div>
      <div className={styles.field}>
        <span className={styles.fieldLabel}>{t.adv.zones}</span>
        <div className={styles.chips} role="group" aria-label={`${name} ${t.adv.zones}`}>
          {ZONE_INDEXES.map((z) => (
            <button
              key={z}
              type="button"
              className={styles.zoneChip}
              aria-pressed={p.zones.includes(z)}
              disabled={p.disabled}
              onClick={() => toggle(z)}
            >
              {t.adv.zoneChip(z)}
            </button>
          ))}
        </div>
        {p.enabled && p.zones.length === 0 && (
          <p className={styles.warnLine} role="status">
            <AlertTriangle size={16} aria-hidden />
            {t.adv.noZone}
          </p>
        )}
      </div>
      <Seconds
        label={t.adv.presence}
        value={p.timing[0]}
        base={p.timingBase[0]}
        disabled={p.disabled}
        onChange={(v) => p.onTiming([v, p.timing[1]])}
      />
      <Seconds
        label={t.adv.absence}
        value={p.timing[1]}
        base={p.timingBase[1]}
        disabled={p.disabled}
        onChange={(v) => p.onTiming([p.timing[0], v])}
      />
    </section>
  )
}
