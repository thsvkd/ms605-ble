import { RotateCcw } from 'lucide-react'
import { type KeyboardEvent, type PointerEvent, useEffect, useId, useRef, useState } from 'react'
import { axisFor, clampThreshold, keyStep, THRESHOLD_UI_MAX, valueAt } from '../../draft'
import { useStrings } from '../../strings'
import styles from './edit.module.css'

interface Props {
  /** 재실 트리거 / 재실 유지 */
  label: string
  /** The draft value (edit ?? device). */
  value: number
  /** The device's tag51 value right now. */
  base: number
  /** The live LiveZone.trigger / maintain, null without a frame. */
  live: number | null
  onChange: (v: number) => void
  disabled?: boolean
}

interface Drag {
  pointerId: number
  rect: { left: number; width: number }
  axis: number
  start: number
  last: number
  startX: number
  /** A touch that has not moved sideways yet: a page scroll (pointercancel) or a tap (pointerup) never edits. */
  pending: boolean
  /** Sideways travel (px) that turns this touch into a drag: 1 on the handle, TOUCH_SLOP_PX elsewhere. */
  slop: number
  /** Handle centre minus the grab point when the handle was grabbed, so the value moves by the travel, not to the finger. */
  offset: number
}

/** Sideways travel (px) before a touch counts as a drag, so a vertical scroll that starts on the track never edits. */
const TOUCH_SLOP_PX = 8
/** A touch this close (px) to the handle's centre grabs it: any sideways move drags (a 48 px target). */
const HANDLE_GRAB_PX = 24

/**
 * The editable threshold (G31): a linear axis where position = value, so the handle can be dragged.
 * The live fill keeps moving underneath and turns red when it is above the new threshold, the same
 * comparison as the monitor meter (value > threshold). An ARIA slider with the 15.9.5 key table.
 */
export function ThresholdMeter({ label, value, base, live, onChange, disabled = false }: Props) {
  const t = useStrings()
  const labelId = useId()
  const captionId = useId()
  const trackRef = useRef<HTMLDivElement>(null)
  const drag = useRef<Drag | null>(null)
  const [frozenAxis, setFrozenAxis] = useState<number | null>(null)
  const [text, setText] = useState(String(value))
  const editing = useRef(false)

  const max = Math.max(THRESHOLD_UI_MAX, base)
  const axis = frozenAxis ?? axisFor(base, value, live)
  const over = live !== null && live > value
  const changed = value !== base
  const pct = (v: number) => `${Math.min(1, Math.max(0, v / axis)) * 100}%`

  useEffect(() => {
    if (!editing.current) setText(String(value))
  }, [value])

  const emit = (v: number) => {
    const next = clampThreshold(v, base)
    if (next !== value) onChange(next)
  }

  const end = () => {
    drag.current = null
    setFrozenAxis(null)
  }

  /** The browser took the gesture (e.g. a page scroll): nothing of this drag stays in the draft. */
  const cancel = () => {
    const d = drag.current
    end()
    if (d && d.last !== d.start) onChange(d.start)
  }

  const follow = (d: Drag, clientX: number) => {
    const v = valueAt(clientX + d.offset, d.rect, d.axis, max)
    if (v === d.last) return
    d.last = v
    onChange(v)
  }

  const activate = (e: PointerEvent<HTMLDivElement>, d: Drag) => {
    d.pending = false
    e.currentTarget.setPointerCapture?.(e.pointerId)
    trackRef.current?.focus()
    follow(d, e.clientX)
  }

  const down = (e: PointerEvent<HTMLDivElement>) => {
    if (disabled || e.button !== 0 || !trackRef.current) return
    const r = trackRef.current.getBoundingClientRect()
    const rect = { left: r.left, width: r.width }
    const touch = e.pointerType === 'touch'
    const handleX = rect.left + Math.min(1, Math.max(0, value / axis)) * rect.width
    const onHandle = Math.abs(e.clientX - handleX) <= HANDLE_GRAB_PX
    const slop = onHandle ? 1 : TOUCH_SLOP_PX
    const offset = touch && onHandle ? handleX - e.clientX : 0 // a mouse press still puts the handle where it lands
    const d = { pointerId: e.pointerId, rect, axis, start: value, last: value, startX: e.clientX, pending: touch, slop, offset }
    drag.current = d
    setFrozenAxis(axis)
    if (!touch) activate(e, d)
  }

  const move = (e: PointerEvent<HTMLDivElement>) => {
    const d = drag.current
    if (!d || d.pointerId !== e.pointerId) return
    if (!d.pending) follow(d, e.clientX)
    else if (Math.abs(e.clientX - d.startX) >= d.slop) activate(e, d)
  }

  const key = (e: KeyboardEvent<HTMLDivElement>) => {
    if (disabled) return
    if (e.key === 'Escape' && drag.current) {
      e.preventDefault()
      cancel()
      return
    }
    const step = keyStep(e.key, e.shiftKey)
    if (step === null) return
    e.preventDefault()
    emit(step === 'min' ? 0 : step === 'max' ? max : value + step)
  }

  const commitText = () => {
    editing.current = false
    const n = Number(text.trim())
    if (text.trim() === '' || !Number.isFinite(n)) {
      setText(String(value))
      return
    }
    const next = clampThreshold(n, base)
    setText(String(next))
    if (next !== value) onChange(next)
  }

  return (
    <div className={styles.tmeter} data-over={over} data-changed={changed} data-disabled={disabled}>
      <span id={labelId} className={styles.tmLabel}>
        {label}
      </span>
      <div
        ref={trackRef}
        className={styles.tmTrack}
        role="slider"
        tabIndex={disabled ? -1 : 0}
        aria-labelledby={labelId}
        aria-valuemin={0}
        aria-valuemax={max}
        aria-valuenow={value}
        aria-valuetext={t.edit.sliderText(value, base, live === null ? null : over)}
        aria-describedby={captionId}
        aria-disabled={disabled || undefined}
        onPointerDown={down}
        onPointerMove={move}
        onPointerUp={end}
        onPointerCancel={cancel}
        onLostPointerCapture={end}
        onKeyDown={key}
      >
        <span className={styles.tmBar} aria-hidden>
          {live !== null && <span className={styles.tmFill} style={{ width: pct(live) }} />}
        </span>
        <span className={styles.tmBase} style={{ left: pct(base) }} aria-hidden />
        <span className={styles.tmHandle} style={{ left: pct(value) }} aria-hidden>
          {changed && <span className={styles.tmBubble}>{value}</span>}
        </span>
      </div>
      <input
        className={styles.tmInput}
        type="number"
        inputMode="numeric"
        min={0}
        max={max}
        step={1}
        aria-label={t.edit.exact(label)}
        value={text}
        disabled={disabled}
        onChange={(e) => {
          editing.current = true
          setText(e.target.value)
        }}
        onBlur={commitText}
        onKeyDown={(e) => {
          if (e.key === 'Enter') {
            e.preventDefault()
            commitText()
          }
        }}
      />
      {changed ? (
        <button
          type="button"
          className={styles.tmReset}
          aria-label={t.edit.resetOne(label)}
          disabled={disabled}
          onClick={() => onChange(base)}
        >
          <RotateCcw size={16} aria-hidden />
        </button>
      ) : (
        <span className={styles.tmResetSlot} aria-hidden />
      )}
      <span id={captionId} className={styles.tmCaption}>
        {live !== null && (
          <>
            <span className={styles.tmLive} data-over={over}>
              {t.edit.capLive(live)}
            </span>
            {' · '}
          </>
        )}
        {t.edit.caption(null, base, value)}
      </span>
    </div>
  )
}
