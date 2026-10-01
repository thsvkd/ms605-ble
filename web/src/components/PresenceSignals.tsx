import { CircleHelp, Eye, EyeOff, Radio, RadioOff, UserRound, UserRoundX, type LucideIcon } from 'lucide-react'
import type { ReactNode } from 'react'
import { type Presence, type SignalKind, signalTone, type Tri } from '../presence'
import { t } from '../strings'
import styles from './presence.module.css'

type State = 'on' | 'off' | 'unknown'

const state = (v: Tri): State => (v === null ? 'unknown' : v ? 'on' : 'off')

const TEXT = { pir: t.presence.pir, rf: t.presence.rf, presence: t.presence.present } as const
const ARIA = { pir: t.presence.ariaPir, rf: t.presence.ariaRf, presence: t.presence.ariaPresent } as const

/** Spoken and tooltip text for one signal; unknown says why ("아직 받은 값 없음"). */
export function signalAria(kind: SignalKind, value: Tri): string {
  return ARIA[kind][state(value)]
}

function signalIcon(kind: SignalKind, value: Tri): LucideIcon {
  if (value === null) return CircleHelp
  if (kind === 'pir') return value ? Eye : EyeOff
  if (kind === 'rf') return value ? Radio : RadioOff
  return value ? UserRound : UserRoundX
}

/**
 * One signal as a chip: colour + icon + text, and the full meaning as the accessible name and tooltip.
 * `short` (monitor rows) shows only the signal's name; the icon and colour carry the state.
 */
export function SignalChip({ kind, value, short = false }: { kind: SignalKind; value: Tri; short?: boolean }) {
  const Icon = signalIcon(kind, value)
  const aria = signalAria(kind, value)
  const text = short && kind !== 'presence' ? t.presence.short[kind] : TEXT[kind][state(value)]
  return (
    <span
      className={styles.chip}
      data-tone={signalTone(kind, value)}
      data-kind={kind}
      data-short={short}
      role="img"
      aria-label={aria}
      title={aria}
    >
      <Icon size={short ? 12 : 14} aria-hidden />
      <span aria-hidden>{text}</span>
    </span>
  )
}

/** S1..S3, the sub-sensor calls behind 재실, as three numbered boxes. */
export function SubBoxes({ subs }: { subs: boolean[] | null }) {
  const aria = subs ? subs.map((on, i) => t.presence.sub(i + 1, on)).join(', ') : t.presence.subsUnknown
  return (
    <span className={styles.subs} role="img" aria-label={aria} title={aria}>
      {(subs ?? [null, null, null]).map((on, i) => (
        <span key={i} className={styles.sub} data-on={on === null ? 'unknown' : on} aria-hidden>
          S{i + 1}
        </span>
      ))}
    </span>
  )
}

/** The card's final answer: the device's own 재실 call, larger and apart from the raw PIR / RF chips. */
function PresenceVerdict({ value, subs }: { value: Tri; subs: boolean[] | null }) {
  const Icon = signalIcon('presence', value)
  const aria = signalAria('presence', value)
  return (
    <div className={styles.verdict} data-tone={signalTone('presence', value)}>
      <span className={styles.verdictMain} role="img" aria-label={aria} title={aria}>
        <Icon size={22} strokeWidth={2.25} aria-hidden />
        <span className={styles.verdictWord} aria-hidden>
          {t.presence.present[state(value)]}
        </span>
      </span>
      <span className={styles.verdictFoot}>
        <span className={styles.verdictNote} title={t.presence.deviceNote}>
          {t.presence.device}
        </span>
        <SubBoxes subs={subs} />
      </span>
    </div>
  )
}

/** The device's 재실 call as the answer, with the raw PIR / RF chips beside it (dashboard card, sensor detail). */
export function PresencePanel({ presence: p }: { presence: Presence }) {
  return (
    <div className={styles.panel}>
      <PresenceVerdict value={p.present} subs={p.subs} />
      <div className={styles.raw}>
        <span className={styles.rawLabel}>{t.presence.raw}</span>
        <SignalChip kind="pir" value={p.pir} />
        <SignalChip kind="rf" value={p.rf} />
      </div>
    </div>
  )
}

/** A legend key: the chip as drawn, with its name for screen readers (the chip's own state label would mislead). */
function LegendKey({ kind, value, name }: { kind: SignalKind; value: Tri; name: string }) {
  return (
    <dt>
      <span aria-hidden>
        <SignalChip kind={kind} value={value} short />
      </span>
      <span className="visually-hidden">{name}</span>
    </dt>
  )
}

/** The definitions, once per screen. `extra` adds screen-specific rows (the monitor's meter key). */
export function PresenceLegend({ extra }: { extra?: ReactNode }) {
  return (
    <dl className={styles.legend} aria-label={t.presence.legendLabel}>
      <div>
        <LegendKey kind="pir" value name={t.presence.short.pir} />
        <dd>{t.presence.legendPir}</dd>
      </div>
      <div>
        <LegendKey kind="rf" value name={t.presence.short.rf} />
        <dd>{t.presence.legendRf}</dd>
      </div>
      <div>
        <LegendKey kind="presence" value name={t.presence.short.presence} />
        <dd>{t.presence.legendPresence}</dd>
      </div>
      <div>
        <LegendKey kind="presence" value={null} name="?" />
        <dd>{t.presence.legendUnknown}</dd>
      </div>
      <div>
        <dt className={styles.legendKey}>{t.presence.colourKey}</dt>
        <dd>{t.presence.legendColour}</dd>
      </div>
      {extra}
    </dl>
  )
}

/** A text key for a legend row (`extra`). */
export function LegendTextRow({ term, children }: { term: string; children: ReactNode }) {
  return (
    <div>
      <dt className={styles.legendKey}>{term}</dt>
      <dd>{children}</dd>
    </div>
  )
}
