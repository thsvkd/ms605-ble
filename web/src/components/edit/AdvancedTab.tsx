import { Radio } from 'lucide-react'
import { baseSection, draftSection, setSection, SUBSENSORS } from '../../draft'
import { t } from '../../strings'
import { DndSwitch } from './DndSwitch'
import { EditFrame } from './EditFrame'
import styles from './edit.module.css'
import { SubSensorEditor } from './SubSensorEditor'
import { TimeSync } from './TimeSync'

const INDEXES = Array.from({ length: SUBSENSORS }, (_, i) => i)

/** 고급: sub-sensors and DND on the same draft and flow as 설정 (15.9.7); time sync is an action. */
export function AdvancedTab({ deviceId }: { deviceId: string }) {
  return (
    <EditFrame deviceId={deviceId}>
      {(d, disabled, update) => {
        const enabled = draftSection(d, 'subsensor_enable')
        const zones = draftSection(d, 'subsensor_zones')
        const timing = draftSection(d, 'subsensor_timing')
        const timingBase = baseSection(d, 'subsensor_timing')
        const enabledBase = baseSection(d, 'subsensor_enable')
        const at = <T,>(list: T[], i: number, v: T) => list.map((x, j) => (j === i ? v : x))
        return (
          <>
            <h2 className={styles.sectionTitle}>
              <Radio size={18} aria-hidden />
              {t.adv.subsensors}
            </h2>
            {INDEXES.map((i) => (
              <SubSensorEditor
                key={i}
                index={i}
                enabled={enabled[i] ?? false}
                enabledBase={enabledBase[i] ?? false}
                zones={zones[i] ?? []}
                timing={timing[i] ?? [0, 0]}
                timingBase={timingBase[i] ?? [0, 0]}
                disabled={disabled}
                onEnabled={(v) => update((x) => setSection(x, 'subsensor_enable', at(draftSection(x, 'subsensor_enable'), i, v)))}
                onZones={(f) =>
                  update((x) => {
                    const all = draftSection(x, 'subsensor_zones')
                    return setSection(x, 'subsensor_zones', at(all, i, f(all[i] ?? [])))
                  })
                }
                onTiming={(v) =>
                  update((x) => setSection(x, 'subsensor_timing', at(draftSection(x, 'subsensor_timing'), i, v)))
                }
              />
            ))}
            <DndSwitch
              value={draftSection(d, 'dnd')}
              base={baseSection(d, 'dnd')}
              disabled={disabled}
              onChange={(v) => update((x) => setSection(x, 'dnd', v))}
            />
            <TimeSync deviceId={deviceId} disabled={disabled} />
          </>
        )
      }}
    </EditFrame>
  )
}
