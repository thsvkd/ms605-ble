import { ChevronRight } from 'lucide-react'
import { useId } from 'react'
import type { BulkDraft } from '../../draft'
import { t } from '../../strings'
import cal from '../calibrate/calibrate.module.css'
import styles from './edit.module.css'
import { Switch } from './Switch'

const ALL_ON = [true, true, true, true, true, true, true]
const DND: { value: boolean | null; label: string }[] = [
  { value: null, label: t.bulk.keep },
  { value: true, label: t.bulk.dndOn },
  { value: false, label: t.bulk.dndOff },
]

/** 공통 설정 (folded): sensitivity, zone on/off for all 7, DND. No sub-sensors here (per sensor, 고급). */
export function CommonSettings({ bulk, onChange }: { bulk: BulkDraft; onChange: (b: BulkDraft) => void }) {
  const id = useId()
  const set = (patch: Partial<BulkDraft>) => onChange({ ...bulk, ...patch })
  const open = bulk.sensitivity !== null || bulk.zone_enable !== null || bulk.dnd !== null
  return (
    <details className={`${styles.section} ${cal.details}`} open={open || undefined}>
      <summary>
        <ChevronRight size={16} className={cal.chevron} aria-hidden />
        {t.bulk.common}
      </summary>
      <div className={styles.field}>
        <label htmlFor={`${id}-s`} className={styles.fieldLabel}>
          {t.section.sensitivity}
        </label>
        <select
          id={`${id}-s`}
          className={styles.select}
          value={bulk.sensitivity ?? ''}
          onChange={(e) => set({ sensitivity: e.target.value === '' ? null : Number(e.target.value) })}
        >
          <option value="">{t.bulk.keep}</option>
          {[1, 2, 3, 4].map((v) => (
            <option key={v} value={v}>
              {t.sens[v]}
            </option>
          ))}
        </select>
      </div>
      <div className={styles.field}>
        <label htmlFor={`${id}-z`} className={styles.fieldLabel}>
          {t.bulk.zoneEnable}
        </label>
        <select
          id={`${id}-z`}
          className={styles.select}
          value={bulk.zone_enable === null ? 'keep' : 'pick'}
          onChange={(e) => set({ zone_enable: e.target.value === 'keep' ? null : [...ALL_ON] })}
        >
          <option value="keep">{t.bulk.keep}</option>
          <option value="pick">{t.bulk.zoneEnablePick}</option>
        </select>
        {bulk.zone_enable && (
          <>
            <p className={styles.note}>{t.bulk.zoneEnableHint}</p>
            <div className={styles.switchGrid}>
              {bulk.zone_enable.map((on, i) => (
                <Switch
                  key={i}
                  checked={on}
                  label={t.edit.zoneSwitch(i)}
                  text={`Z${i} ${on ? t.edit.zoneOn : t.edit.zoneOff}`}
                  onChange={(v) => set({ zone_enable: bulk.zone_enable?.map((x, j) => (j === i ? v : x)) ?? null })}
                />
              ))}
            </div>
          </>
        )}
      </div>
      <div className={styles.field}>
        <span className={styles.fieldLabel} aria-hidden>
          {t.section.dnd}
        </span>
        <fieldset className={cal.segmented}>
          <legend className="visually-hidden">{t.section.dnd}</legend>
          {DND.map((o) => (
            <label key={String(o.value)} className={cal.segment}>
              <input
                type="radio"
                name={`${id}-d`}
                checked={bulk.dnd === o.value}
                onChange={() => set({ dnd: o.value })}
              />
              {o.label}
            </label>
          ))}
        </fieldset>
      </div>
    </details>
  )
}
