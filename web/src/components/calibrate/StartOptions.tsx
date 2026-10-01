import { Info } from 'lucide-react'
import { useId } from 'react'
import type { StartBody } from '../../api/client'
import type { StartMode } from '../../api/types'
import { formatHHMM, resolveAt } from '../../calibration'
import { t } from '../../strings'
import styles from './calibrate.module.css'

export interface StartChoice {
  mode: StartMode
  /** The raw input, so a half-typed value is not rewritten under the cursor. */
  delayS: string
  /** HH:MM */
  at: string
}

const QUICK_S = [10, 30, 60, 120]
const MODE_LABEL: Record<StartMode, string> = { now: t.start.now, delay: t.start.delay, at: t.start.at }

/** Ten minutes from now, rounded up to the next 5-minute mark. */
export function defaultAt(now: Date): string {
  const d = new Date(now.getTime() + 10 * 60_000)
  const extra = (5 - (d.getMinutes() % 5)) % 5
  d.setMinutes(d.getMinutes() + extra, 0, 0)
  return formatHHMM(d)
}

export function defaultChoice(now: Date, mode: StartMode = 'now'): StartChoice {
  return { mode, delayS: '30', at: defaultAt(now) }
}

function delayValue(raw: string): number | null {
  const n = Number(raw)
  return Number.isInteger(n) && n >= 1 && n <= 3600 ? n : null
}

/** `오늘 14:30` / `내일 07:00` */
export function atLabel(hhmm: string, now: Date): string {
  const at = resolveAt(hhmm, now)
  return at.getDate() === now.getDate() ? t.start.atToday(hhmm) : t.start.atTomorrow(hhmm)
}

/** The request fields for this choice, or null while the input is invalid. */
export function startBody(choice: StartChoice, now: Date): StartBody | null {
  if (choice.mode === 'now') return { start: 'now' }
  if (choice.mode === 'delay') {
    const s = delayValue(choice.delayS)
    return s === null ? null : { start: 'delay', delay_s: s }
  }
  if (!/^\d{2}:\d{2}$/.test(choice.at)) return null
  return { start: 'at', at: resolveAt(choice.at, now).toISOString() }
}

/** The primary button: 보정 시작 / 30초 후 보정 시작 / 오늘 14:30에 보정 예약. */
export function startLabel(choice: StartChoice, now: Date): string {
  if (choice.mode === 'delay') return t.start.goDelay(delayValue(choice.delayS) ?? 0)
  if (choice.mode === 'at') return t.start.goAt(/^\d{2}:\d{2}$/.test(choice.at) ? atLabel(choice.at, now) : '—')
  return t.start.goNow
}

interface Props {
  value: StartChoice
  onChange: (next: StartChoice) => void
  modes?: readonly StartMode[]
  now: Date
}

/** 지금 / N초 후 / 시각 예약, as a segmented radio group. */
export function StartOptions({ value, onChange, modes = ['now', 'delay', 'at'], now }: Props) {
  const name = useId()
  const delayId = useId()
  const atId = useId()
  const set = (patch: Partial<StartChoice>) => onChange({ ...value, ...patch })
  const delay = delayValue(value.delayS)

  return (
    <div className={styles.startDetail}>
      <fieldset className={styles.segmented}>
        <legend className="visually-hidden">{t.start.title}</legend>
        {modes.map((m) => (
          <label key={m} className={styles.segment}>
            <input type="radio" name={name} value={m} checked={value.mode === m} onChange={() => set({ mode: m })} />
            {MODE_LABEL[m]}
          </label>
        ))}
      </fieldset>

      {value.mode === 'delay' && (
        <div className={styles.inline}>
          <label htmlFor={delayId} className="visually-hidden">
            {t.start.delay}
          </label>
          <input
            id={delayId}
            className={styles.number}
            type="number"
            inputMode="numeric"
            min={1}
            max={3600}
            step={1}
            value={value.delayS}
            aria-invalid={delay === null}
            onChange={(e) => set({ delayS: e.target.value })}
          />
          <span>{t.start.seconds}</span>
          {QUICK_S.map((s) => (
            <button
              key={s}
              type="button"
              className={styles.quick}
              aria-pressed={delay === s}
              onClick={() => set({ delayS: String(s) })}
            >
              {t.start.secondsN(s)}
            </button>
          ))}
        </div>
      )}

      {value.mode === 'at' && (
        <>
          <div className={styles.inline}>
            <label htmlFor={atId} className="visually-hidden">
              {t.start.at}
            </label>
            <input
              id={atId}
              className={styles.number}
              type="time"
              value={value.at}
              onChange={(e) => set({ at: e.target.value })}
            />
            {/^\d{2}:\d{2}$/.test(value.at) && <strong>{atLabel(value.at, now)}</strong>}
          </div>
          <p className={styles.leaveRoom}>
            <Info size={16} aria-hidden />
            {t.start.atNote}
          </p>
        </>
      )}
    </div>
  )
}
