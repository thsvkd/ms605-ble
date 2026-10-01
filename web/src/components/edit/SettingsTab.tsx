import { Copy, Gauge, Rows3 } from 'lucide-react'
import { useId } from 'react'
import { Link } from 'wouter'
import { baseSection, baseThreshold, draftSection, draftThreshold, setSection, setThreshold } from '../../draft'
import { useLiveWatch } from '../../hooks/useLiveWatch'
import { useStore } from '../../store/store'
import { t } from '../../strings'
import cal from '../calibrate/calibrate.module.css'
import { EditFrame } from './EditFrame'
import styles from './edit.module.css'
import { ZoneEditRow } from './ZoneEditRow'

const SENSITIVITIES = [1, 2, 3, 4] as const

/** 설정: sensitivity and the seven zones, each threshold draggable over its live fill (15.9.6). */
export function SettingsTab({ deviceId }: { deviceId: string }) {
  useLiveWatch([deviceId])
  const frame = useStore((s) => s.live[deviceId])
  const radio = useId()

  return (
    <EditFrame deviceId={deviceId}>
      {(d, disabled, update) => {
        const sensitivity = draftSection(d, 'sensitivity')
        const enabled = draftSection(d, 'zone_enable')
        const enabledBase = baseSection(d, 'zone_enable')
        return (
          <>
            <section className={styles.section} aria-labelledby={`${radio}-s`}>
              <h2 id={`${radio}-s`} className={styles.sectionTitle}>
                <Gauge size={18} aria-hidden />
                {t.edit.sensitivity}
              </h2>
              <fieldset className={cal.segmented} disabled={disabled}>
                <legend className="visually-hidden">{t.edit.sensitivity}</legend>
                {SENSITIVITIES.map((v) => (
                  <label key={v} className={cal.segment}>
                    <input
                      type="radio"
                      name={radio}
                      value={v}
                      checked={sensitivity === v}
                      onChange={() => update((x) => setSection(x, 'sensitivity', v))}
                    />
                    {t.sens[v]}
                  </label>
                ))}
              </fieldset>
              <p className={styles.note}>{t.edit.sensitivityNote}</p>
            </section>

            <section className={styles.section} aria-labelledby={`${radio}-z`}>
              <h2 id={`${radio}-z`} className={styles.sectionTitle}>
                <Rows3 size={18} aria-hidden />
                {t.edit.zones}
              </h2>
              <p className={styles.legend}>{t.edit.legend}</p>
              <ul className={styles.zones}>
                {enabled.map((on, i) => (
                  <ZoneEditRow
                    key={i}
                    index={i}
                    distanceM={d.base.distances_m[i]}
                    enabled={on}
                    enabledBase={enabledBase[i] ?? on}
                    trigger={{ value: draftThreshold(d, i, 'trigger'), base: baseThreshold(d, i, 'trigger') }}
                    maintain={{ value: draftThreshold(d, i, 'maintain'), base: baseThreshold(d, i, 'maintain') }}
                    live={frame?.zones[i]}
                    disabled={disabled}
                    onEnabled={(v) =>
                      update((x) =>
                        setSection(x, 'zone_enable', draftSection(x, 'zone_enable').map((e, j) => (j === i ? v : e))),
                      )
                    }
                    onTrigger={(v) => update((x) => setThreshold(x, i, 'trigger', v))}
                    onMaintain={(v) => update((x) => setThreshold(x, i, 'maintain', v))}
                  />
                ))}
              </ul>
            </section>

            <Link href={`/bulk?mode=clone&source=${encodeURIComponent(deviceId)}`} className={styles.link}>
              <Copy size={16} aria-hidden />
              {t.edit.cloneTo}
            </Link>
          </>
        )
      }}
    </EditFrame>
  )
}
