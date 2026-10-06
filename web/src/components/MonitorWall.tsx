import { ChevronDown, CircleHelp, Eye, Hourglass, Radio, Unplug, UserRound } from 'lucide-react'
import {
  type ButtonHTMLAttributes,
  type CSSProperties,
  memo,
  type PointerEvent as ReactPointerEvent,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
} from 'react'
import { Link } from 'wouter'
import type { LiveZone } from '../api/types'
import { useNow } from '../hooks/useNow'
import { presenceOf, presenceTally, type SignalKind, signalTone, type Tri } from '../presence'
import { sensorStatus } from '../status'
import { sensorName, useStore } from '../store/store'
import { t, useStrings } from '../strings'
import { LegendTextRow, PresenceLegend, SignalChip, signalAria, signalIcon, SubBoxes } from './PresenceSignals'
import styles from './monitor.module.css'

const ZONES = [0, 1, 2, 3, 4, 5, 6] // the device's seven radar zones (LiveData.zones)

const state = (v: Tri) => (v === null ? 'unknown' : v ? 'on' : 'off')

/**
 * "N 재실 / M대 · N PIR 감지 · N RF 감지", over the watched sensors' latest frames. Only a connected sensor
 * counts: the last frame of a lost or released one is history, so it shows as "연결 끊김" instead.
 */
export function MonitorSummary({ ids }: { ids: readonly string[] }) {
  const t = useStrings()
  const live = useStore((s) => s.live)
  const sensors = useStore((s) => s.sensors)
  const c = useMemo(
    () => presenceTally(ids.map((id) => ({ frame: live[id], connected: sensors[id]?.live?.link === 'connected' }))),
    [ids, live, sensors],
  )
  const stats = [
    { key: 'present', n: c.present, tone: 'present', Icon: UserRound, label: t.monitor.summaryPresent(c.total) },
    { key: 'pir', n: c.pir, tone: 'hit', Icon: Eye, label: t.monitor.summaryPir },
    { key: 'rf', n: c.rf, tone: 'hit', Icon: Radio, label: t.monitor.summaryRf },
  ]
  return (
    <ul className={styles.summary} aria-label={t.monitor.summaryLabel}>
      {stats.map(({ key, n, tone, Icon, label }) => (
        <li key={key} className={styles.stat} data-tone={n > 0 ? tone : 'none'}>
          <Icon size={16} aria-hidden />
          <b>{n}</b>
          <span>{label}</span>
        </li>
      ))}
      {c.unknown > 0 && (
        <li className={styles.stat} data-tone="unknown">
          <CircleHelp size={16} aria-hidden />
          <b>{c.unknown}</b>
          <span>{t.monitor.summaryUnknown}</span>
        </li>
      )}
      {c.offline > 0 && (
        <li className={styles.stat} data-tone="offline">
          <Unplug size={16} aria-hidden />
          <b>{c.offline}</b>
          <span>{t.monitor.summaryOffline}</span>
        </li>
      )}
    </ul>
  )
}

/**
 * The keys a tile needs, in at most three lines on a phone; the full definitions (PresenceLegend plus the
 * meter and zone rows) open below on demand.
 */
export function MonitorLegend() {
  const t = useStrings()
  const [open, setOpen] = useState(false)
  const id = useId()
  return (
    <div className={styles.legend}>
      <p className={styles.legendShort}>
        <span className={styles.legendItem}>
          <span aria-hidden className={styles.legendChips}>
            <SignalChip kind="pir" value short />
            <SignalChip kind="rf" value short />
          </span>
          <span className="visually-hidden">{`${t.presence.short.pir}, ${t.presence.short.rf}: `}</span>
          {t.monitor.legendRaw}
        </span>
        <span className={styles.legendItem}>
          <span aria-hidden>
            <SignalChip kind="presence" value short />
          </span>
          <span className="visually-hidden">{`${t.presence.short.presence}: `}</span>
          {t.monitor.legendDevice}
        </span>
        <span className={styles.legendItem}>
          <span className={styles.glyph} aria-hidden />
          {t.monitor.legendMeterShort}
        </span>
      </p>
      <button
        type="button"
        className={styles.legendToggle}
        aria-expanded={open}
        aria-controls={id}
        onClick={() => setOpen((o) => !o)}
      >
        {t.monitor.legendMore}
        <ChevronDown size={14} aria-hidden />
      </button>
      <div id={id} className={styles.legendFull} hidden={!open}>
        {open && (
          <PresenceLegend
            extra={
              <>
                <LegendTextRow term={t.monitor.legendMeterKey}>{t.monitor.legendMeter}</LegendTextRow>
                <LegendTextRow term={t.monitor.legendZoneKey}>{t.monitor.legendZone}</LegendTextRow>
              </>
            }
          />
        )}
      </div>
    </div>
  )
}

/** One tile per watched sensor: 4 columns on a wide screen, 3 at medium widths, 2 on a phone. */
export function MonitorWall({ ids }: { ids: readonly string[] }) {
  const t = useStrings()
  return (
    <ul className={styles.wall} aria-label={t.monitor.wallLabel}>
      {ids.map((id) => (
        <MonitorTile key={id} deviceId={id} />
      ))}
    </ul>
  )
}

/**
 * The device's 재실 call first and large, then the raw PIR / RF signals, then Z0..Z6 as vertical trigger
 * meters. A lost sensor keeps its last frame, dimmed and never tinted blue.
 */
export const MonitorTile = memo(function MonitorTile({ deviceId }: { deviceId: string }) {
  const t = useStrings()
  const sensor = useStore((s) => s.sensors[deviceId])
  const frame = useStore((s) => s.live[deviceId])
  const gathering = useStore((s) => s.gather.gathering)
  const now = useNow()
  const nameId = useId()
  if (!sensor?.live) return null

  const status = sensorStatus(sensor, gathering, now)
  const connected = sensor.live.link === 'connected'
  const calibrating = connected && sensor.live.busy === 'calibration'
  const location = sensor.registry?.location
  const p = presenceOf(frame)
  const VerdictIcon = signalIcon('presence', p.present)
  const verdictAria = signalAria('presence', p.present)

  return (
    <li
      className={styles.tile}
      aria-labelledby={nameId}
      data-tone={connected ? signalTone('presence', p.present) : 'stale'}
      data-present={connected && p.present === true}
      data-stale={!connected}
    >
      <div className={styles.head}>
        <h2 id={nameId} className={styles.name}>
          <Link href={`/sensors/${deviceId}`} className={styles.nameLink} title={sensorName(sensor, deviceId)}>
            {sensorName(sensor, deviceId)}
          </Link>
        </h2>
        {location && <span className={styles.location}>{location}</span>}
      </div>
      {!connected && (
        <p className={styles.warn}>
          {status.label}
          {status.hint && <span className={styles.hint}>{status.hint}</span>}
          {frame && <span className={styles.hint}>{t.live.stale}</span>}
        </p>
      )}
      {calibrating && (
        <p className={styles.calibrating}>
          <Hourglass size={13} aria-hidden />
          <span>
            {status.label}
            <span className={styles.hint}>{t.live.calibrating}</span>
          </span>
        </p>
      )}
      {connected && !frame && <p className={styles.waiting}>{t.live.waiting}</p>}

      <div className={styles.body}>
        <div className={styles.verdict}>
          <VerdictIcon className={styles.verdictIcon} strokeWidth={2.1} aria-hidden />
          <span className={styles.verdictText}>
            <span className={styles.verdictWord} role="img" aria-label={verdictAria} title={verdictAria}>
              <span aria-hidden>{t.presence.present[state(p.present)]}</span>
            </span>
            <span className={styles.verdictNote} title={t.presence.deviceNote}>
              {t.presence.device}
              <span className="visually-hidden"> · {t.presence.deviceUnsure}</span>
            </span>
          </span>
        </div>
        <SubBoxes subs={p.subs} />
        <div className={styles.signals}>
          <SignalBlock kind="pir" value={p.pir} />
          <SignalBlock kind="rf" value={p.rf} />
        </div>
        <ZoneStrip zones={frame?.zones} />
      </div>
    </li>
  )
})

/** A raw signal as a half-width block: colour + icon + words, red when it fires. */
function SignalBlock({ kind, value }: { kind: Exclude<SignalKind, 'presence'>; value: Tri }) {
  const t = useStrings()
  const Icon = signalIcon(kind, value)
  const aria = signalAria(kind, value)
  return (
    <span className={styles.signal} data-tone={signalTone(kind, value)} role="img" aria-label={aria} title={aria}>
      <Icon size={17} aria-hidden />
      <span aria-hidden>{t.presence[kind][state(value)]}</span>
    </span>
  )
}

/** "Z3 · 3.2 m · 트리거 41/55 · 유지 30/30", or "Z6 · 7.2 m · 꺼짐", as parts for the readout. */
export function zoneDetailParts(z: LiveZone): string[] {
  const where = t.live.zone(z.index, z.distance_m)
  if (!z.enabled) return [where, t.live.zoneOff]
  const parts = [where, t.monitor.zoneTrigger(z.trigger, z.trigger_threshold), t.monitor.zoneMaintain(z.maintain, z.maintain_threshold)]
  // the device's own flag, which is what RF counts; it can differ from the bar's value > threshold
  if (z.trigger_active) parts.push(t.monitor.zoneRf)
  return parts
}

export const zoneDetail = (z: LiveZone) => zoneDetailParts(z).join(' · ')

/** The value range one tile's meters share. */
export interface ZoneScale {
  lo: number
  hi: number
}

/** Round up to a multiple of 5·10^(digits-2): 742 -> 750, 88 -> 90, 3 -> 3. */
function niceCeil(x: number): number {
  const step = Math.max(1, 5 * 10 ** (Math.floor(Math.log10(x)) - 1))
  return Math.ceil(x / step) * step
}

/**
 * One scale for all of a sensor's zones, so each threshold rule stands at its calibrated height and zones
 * compare at a glance. It reaches 1.5x the highest enabled trigger threshold (headroom to see a value cross
 * it) and follows only the thresholds, which change on calibration, never the values: the scale holds still
 * while the bars move. Below 0 only when a threshold is negative (a calibration transient).
 */
export function zoneScale(zones: readonly LiveZone[]): ZoneScale {
  const thresholds = zones.filter((z) => z.enabled).map((z) => z.trigger_threshold)
  const top = Math.max(1, ...thresholds)
  const bottom = Math.min(0, ...thresholds)
  return { lo: bottom < 0 ? -niceCeil(-bottom * 1.5) : 0, hi: niceCeil(top * 1.5) }
}

/** Where `v` stands on the scale, 0..1 (clamped). */
export const scaleAt = (v: number, { lo, hi }: ZoneScale) => Math.min(1, Math.max(0, (v - lo) / (hi - lo)))

/**
 * Z0..Z6 as vertical trigger meters. Each one is a button: hover, focus or a tap shows its numbers above the
 * strip (the readout spans the strip, so it never runs off a narrow tile). A second tap, Escape or a tap
 * elsewhere hides it.
 */
function ZoneStrip({ zones }: { zones: LiveZone[] | undefined }) {
  const t = useStrings()
  const [open, setOpen] = useState<number | null>(null)
  const ref = useRef<HTMLDivElement>(null)
  // where the press started: a touch toggles against the state before its own focus event opened it
  const press = useRef<{ index: number; wasOpen: boolean; mouse: boolean } | null>(null)

  useEffect(() => {
    if (open === null) return
    const away = (e: PointerEvent) => {
      if (!ref.current?.contains(e.target as Node)) setOpen(null)
    }
    document.addEventListener('pointerdown', away)
    return () => document.removeEventListener('pointerdown', away)
  }, [open])

  if (!zones) {
    return (
      <div className={styles.zones}>
        {ZONES.map((i) => (
          <span key={i} className={styles.zone} aria-hidden>
            <span className={styles.track} data-empty />
            <span className={styles.zoneLabel}>Z{i}</span>
          </span>
        ))}
        <span className="visually-hidden">{t.monitor.zonesWaiting}</span>
      </div>
    )
  }

  const shown = open === null ? undefined : zones.find((z) => z.index === open)
  const scale = zoneScale(zones)
  const handlers = (index: number) => ({
    onPointerEnter: (e: ReactPointerEvent<HTMLButtonElement>) => e.pointerType === 'mouse' && setOpen(index),
    onPointerLeave: (e: ReactPointerEvent<HTMLButtonElement>) =>
      e.pointerType === 'mouse' && setOpen((o) => (o === index ? null : o)),
    onPointerDown: (e: ReactPointerEvent<HTMLButtonElement>) => {
      press.current = { index, wasOpen: open === index, mouse: e.pointerType === 'mouse' }
    },
    onFocus: () => setOpen(index),
    onBlur: () => setOpen((o) => (o === index ? null : o)),
    onClick: () => {
      const p = press.current?.index === index ? press.current : null
      press.current = null
      if (p?.mouse) return // the pointer already shows it
      if (!p) return setOpen(index) // Enter / Space: focus already opened it, so keep it open
      setOpen(p.wasOpen ? null : index)
    },
  })

  return (
    <div
      ref={ref}
      className={styles.zones}
      role="group"
      aria-label={t.monitor.zonesLabel}
      onKeyDown={(e) => e.key === 'Escape' && setOpen(null)}
    >
      {shown && (
        <span className={styles.readout} style={{ '--at': shown.index } as CSSProperties} aria-hidden>
          {zoneDetailParts(shown).map((part, i) => (
            <span key={i} className={i === 0 ? styles.readoutWhere : undefined}>
              {i > 0 && <span className={styles.sep}> · </span>}
              {part}
            </span>
          ))}
        </span>
      )}
      {zones.map((z) => (
        <ZoneMeter key={z.index} zone={z} scale={scale} active={open === z.index} {...handlers(z.index)} />
      ))}
    </div>
  )
}

type ZoneMeterProps = {
  zone: LiveZone
  scale: ZoneScale
  active: boolean
} & Pick<
  ButtonHTMLAttributes<HTMLButtonElement>,
  'onPointerEnter' | 'onPointerLeave' | 'onPointerDown' | 'onFocus' | 'onBlur' | 'onClick'
>

/** One zone: its trigger meter (or hatching when off) and Z{i}; the accessible name carries the numbers. */
function ZoneMeter({ zone: z, scale, active, ...on }: ZoneMeterProps) {
  const t = useStrings()
  const detail = zoneDetail(z)
  const over = z.enabled ? z.trigger > z.trigger_threshold : undefined
  return (
    <button
      type="button"
      className={styles.zone}
      data-active={active}
      data-off={!z.enabled || undefined}
      data-over={over}
      data-rf={(z.enabled && z.trigger_active) || undefined}
      aria-label={over === undefined ? detail : `${detail}, ${t.monitor.zoneOver(over)}`}
      {...on}
    >
      {z.enabled ? (
        <VerticalMeter value={z.trigger} threshold={z.trigger_threshold} scale={scale} />
      ) : (
        <span className={styles.track} data-off />
      )}
      <span className={styles.zoneRf} aria-hidden>
        {z.enabled && z.trigger_active && <Radio size={11} />}
      </span>
      <span className={styles.zoneLabel} aria-hidden>
        Z{z.index}
      </span>
    </button>
  )
}

/**
 * A trigger meter on its tile's shared scale: the fill rises to the value (red iff value > threshold) and the
 * rule stands at the calibrated threshold. A value past the top of the scale gets a cap.
 */
export function VerticalMeter({ value, threshold, scale }: { value: number; threshold: number; scale: ZoneScale }) {
  const style = { '--v': scaleAt(value, scale), '--t': scaleAt(threshold, scale) } as CSSProperties
  return (
    <span
      className={styles.track}
      style={style}
      data-over={value > threshold}
      data-clip={value > scale.hi || undefined}
      data-meter="vertical"
    >
      <i className={styles.fill} />
      <i className={styles.rule} />
    </span>
  )
}
