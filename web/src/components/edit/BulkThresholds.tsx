import { AlertTriangle, Minus, Plus, Rows3 } from 'lucide-react'
import { type MouseEvent, useEffect, useId, useState } from 'react'
import { type BulkDraft, setBulkMode, THRESHOLD_UI_MAX, type ThresholdPart, ZONES } from '../../draft'
import { t } from '../../strings'
import cal from '../calibrate/calibrate.module.css'
import styles from './edit.module.css'

const MINUS = '−'

/** `+5`, `−3` (relative); `40` (absolute); '' = unchanged. */
function show(v: number | null, relative: boolean): string {
  if (v === null) return ''
  if (!relative) return String(v)
  return v > 0 ? `+${v}` : v < 0 ? `${MINUS}${-v}` : '0'
}

function parse(text: string): number | null | undefined {
  const s = text.trim().replace(MINUS, '-')
  if (s === '') return null
  if (!/^[+-]?\d+$/.test(s)) return undefined // half typed: keep the text, change nothing
  return Number(s)
}

interface StepProps {
  label: string
  value: number | null
  relative: boolean
  onChange: (v: number | null) => void
}

/** − · number · +, each 48 px; Shift+click steps 10. Blank means 그대로. */
export function StepInput({ label, value, relative, onChange }: StepProps) {
  const [text, setText] = useState(show(value, relative))
  const [focused, setFocused] = useState(false)
  useEffect(() => {
    if (!focused) setText(show(value, relative))
  }, [value, relative, focused])

  const lo = relative ? -THRESHOLD_UI_MAX : 0
  const hi = THRESHOLD_UI_MAX
  const put = (v: number) => {
    const c = Math.min(hi, Math.max(lo, v))
    onChange(relative && c === 0 ? null : c)
  }
  const step = (e: MouseEvent, sign: 1 | -1) => put((value ?? 0) + sign * (e.shiftKey ? 10 : 1))
  // absolute has no per-sensor base to step from: a value is typed first (0 would be a silent overwrite)
  const noStep = !relative && value === null

  return (
    <span className={styles.step} data-set={value !== null}>
      <button type="button" aria-label={t.bulk.dec(label)} disabled={noStep} onClick={(e) => step(e, -1)}>
        <Minus size={16} aria-hidden />
      </button>
      <input
        type="text"
        inputMode={relative ? 'text' : 'numeric'}
        aria-label={label}
        placeholder={t.bulk.keep}
        value={text}
        onFocus={() => setFocused(true)}
        onBlur={() => {
          setFocused(false)
          setText(show(value, relative))
        }}
        onChange={(e) => {
          setText(e.target.value)
          const v = parse(e.target.value)
          if (v === null) onChange(null)
          else if (v !== undefined) put(v)
        }}
      />
      <button type="button" aria-label={t.bulk.inc(label)} disabled={noStep} onClick={(e) => step(e, 1)}>
        <Plus size={16} aria-hidden />
      </button>
    </span>
  )
}

const PARTS: { part: ThresholdPart; label: string }[] = [
  { part: 'trigger', label: t.live.trigger },
  { part: 'maintain', label: t.live.maintain },
]

/** D8: relative ±n by default; absolute is chosen on purpose and must be acknowledged (G29). */
export function BulkThresholds({ bulk, onChange }: { bulk: BulkDraft; onChange: (b: BulkDraft) => void }) {
  const name = useId()
  const relative = bulk.mode === 'relative'
  const setCell = (part: ThresholdPart, zone: number | 'all', v: number | null) =>
    onChange({
      ...bulk,
      [part]: zone === 'all' ? bulk[part].map(() => v) : bulk[part].map((x, i) => (i === zone ? v : x)),
    })
  const allOf = (part: ThresholdPart) => {
    const first = bulk[part][0] ?? null
    return bulk[part].every((v) => v === first) ? first : null
  }

  return (
    <section className={styles.section} aria-labelledby={`${name}-t`}>
      <h2 id={`${name}-t`} className={styles.sectionTitle}>
        <Rows3 size={18} aria-hidden />
        {t.bulk.thresholds}
      </h2>
      <fieldset className={cal.segmented}>
        <legend className="visually-hidden">{t.bulk.thresholds}</legend>
        {(['relative', 'absolute'] as const).map((m) => (
          <label key={m} className={cal.segment}>
            <input
              type="radio"
              name={name}
              value={m}
              checked={bulk.mode === m}
              onChange={() => onChange(setBulkMode(bulk, m))}
            />
            {m === 'relative' ? t.bulk.relative : t.bulk.absolute}
          </label>
        ))}
      </fieldset>
      <p className={styles.note}>{relative ? t.bulk.relativeHint : t.bulk.absoluteHint}</p>
      {!relative && (
        <div className={cal.warning} role="alert">
          <span className={cal.warningIcon} aria-hidden>
            <AlertTriangle size={22} />
          </span>
          <p className={cal.warningBody}>{t.bulk.absoluteWarn}</p>
          <label className={`${cal.override} ${styles.ackRow}`}>
            <input
              type="checkbox"
              checked={bulk.absoluteAck}
              onChange={(e) => onChange({ ...bulk, absoluteAck: e.target.checked })}
            />
            {t.bulk.absoluteAck}
          </label>
        </div>
      )}
      <table className={styles.stepTable}>
        <thead>
          <tr>
            <th scope="col">
              <span className="visually-hidden">{t.history.zone}</span>
            </th>
            {PARTS.map((p) => (
              <th key={p.part} scope="col">
                {p.label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          <tr data-all="true">
            <th scope="row">{t.bulk.allZones}</th>
            {PARTS.map((p) => (
              <td key={p.part} data-label={p.label}>
                <StepInput
                  label={`${t.bulk.allZones} ${p.label}`}
                  value={allOf(p.part)}
                  relative={relative}
                  onChange={(v) => setCell(p.part, 'all', v)}
                />
              </td>
            ))}
          </tr>
          {Array.from({ length: ZONES }, (_, z) => (
            <tr key={z}>
              <th scope="row">Z{z}</th>
              {PARTS.map((p) => (
                <td key={p.part} data-label={p.label}>
                  <StepInput
                    label={`Z${z} ${p.label}`}
                    value={bulk[p.part][z] ?? null}
                    relative={relative}
                    onChange={(v) => setCell(p.part, z, v)}
                  />
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  )
}
