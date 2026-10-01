import { AlertTriangle, Copy } from 'lucide-react'
import { useEffect, useId, useState } from 'react'
import { failureText, getConfig } from '../../api/client'
import type { ConfigView, Section, SensorView } from '../../api/types'
import { sectionLabel } from '../../apply'
import { type CloneDraft, SECTION_ORDER } from '../../draft'
import { sensorName } from '../../store/store'
import { t } from '../../strings'
import cal from '../calibrate/calibrate.module.css'
import { BulkTargets, targetReason } from './BulkTargets'
import styles from './edit.module.css'

interface Props {
  clone: CloneDraft
  sessions: SensorView[]
  batchLocked: ReadonlySet<string>
  applyLocked: ReadonlySet<string>
  gathering: boolean
  onChange: (c: CloneDraft) => void
  /** The source as read for the summary (and its DND support); expect_rev takes the preview's source_rev. */
  onSource: (cfg: ConfigView | null) => void
}

function summaryOf(cfg: ConfigView): string {
  const p = cfg.profile
  const zones = p.zone_thresholds
    .slice(0, 2)
    .map((z, i) => `Z${i} ${z.trigger}/${z.maintain}`)
    .join(' · ')
  return t.clone.summary(t.sens[p.sensitivity] ?? String(p.sensitivity), p.zone_enable.filter(Boolean).length, `${zones} …`)
}

/** Clone = the server reads the source and writes it to the targets as an absolute draft (G36). */
export function CloneSetup({ clone, sessions, batchLocked, applyLocked, gathering, onChange, onSource }: Props) {
  const id = useId()
  const [cfg, setCfg] = useState<ConfigView | null>(null)
  const [error, setError] = useState<string | null>(null)
  const sources = sessions.filter((s) => targetReason(s, batchLocked, applyLocked) === null)

  useEffect(() => {
    setCfg(null)
    setError(null)
    onSource(null)
    if (!clone.source) return
    let live = true
    getConfig(clone.source).then(
      (c) => {
        if (!live) return
        setCfg(c)
        onSource(c)
      },
      (e: unknown) => live && setError(failureText(e)),
    )
    return () => {
      live = false
    }
    // onSource is the parent's state setter: the read depends on the source only
  }, [clone.source])

  const dndUnknown = cfg?.profile.dnd === null
  const toggle = (s: Section) =>
    onChange({
      ...clone,
      sections: clone.sections.includes(s)
        ? clone.sections.filter((x) => x !== s)
        : SECTION_ORDER.filter((x) => x === s || clone.sections.includes(x)),
    })

  return (
    <>
      <section className={styles.section} aria-label={t.clone.source}>
        <div className={styles.field}>
          <label htmlFor={`${id}-src`} className={styles.sectionTitle}>
            <Copy size={18} aria-hidden />
            {t.clone.source}
          </label>
          <select
            id={`${id}-src`}
            className={styles.select}
            value={clone.source ?? ''}
            onChange={(e) => {
              const source = e.target.value || null
              onChange({ ...clone, source, ids: clone.ids.filter((x) => x !== source) })
            }}
          >
            <option value="">{t.clone.pickSource}</option>
            {sources.map((s) => (
              <option key={s.device_id} value={s.device_id}>
                {sensorName(s, s.device_id)}
              </option>
            ))}
          </select>
        </div>
        {cfg && <p className={styles.note}>{summaryOf(cfg)}</p>}
        {error && (
          <p className={styles.error} role="alert">
            {error}
          </p>
        )}
      </section>

      <section className={styles.section} aria-labelledby={`${id}-sec`}>
        <h2 id={`${id}-sec`} className={styles.sectionTitle}>
          {t.clone.sections}
        </h2>
        <div className={styles.switchGrid}>
          {SECTION_ORDER.map((s) => {
            const blocked = s === 'dnd' && dndUnknown
            return (
              <label key={s} className={cal.check} data-disabled={blocked}>
                <input
                  type="checkbox"
                  checked={!blocked && clone.sections.includes(s)}
                  disabled={blocked}
                  onChange={() => toggle(s)}
                />
                <span>{sectionLabel(s)}</span>
              </label>
            )
          })}
        </div>
        {dndUnknown && <p className={styles.note}>{t.clone.dndUnknown}</p>}
        {clone.sections.includes('zone_thresholds') && (
          <p className={styles.warnLine} role="status">
            <AlertTriangle size={16} aria-hidden />
            {t.clone.thresholdWarn}
          </p>
        )}
      </section>

      <BulkTargets
        sessions={sessions}
        selected={clone.ids}
        batchLocked={batchLocked}
        applyLocked={applyLocked}
        gathering={gathering}
        exclude={clone.source}
        onChange={(ids) => onChange({ ...clone, ids })}
      />
    </>
  )
}
