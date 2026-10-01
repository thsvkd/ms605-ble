import type { LiveZone } from '../../api/types'
import { t } from '../../strings'
import styles from './edit.module.css'
import { Switch } from './Switch'
import { ThresholdMeter } from './ThresholdMeter'

interface Pair {
  value: number
  base: number
}

interface Props {
  index: number
  distanceM: number | undefined
  enabled: boolean
  enabledBase: boolean
  trigger: Pair
  maintain: Pair
  live: LiveZone | undefined
  disabled: boolean
  onEnabled: (v: boolean) => void
  onTrigger: (v: number) => void
  onMaintain: (v: number) => void
}

/** One zone: name · distance, the on/off switch, then the trigger and maintain sliders over the live fill. */
export function ZoneEditRow(p: Props) {
  const name = `Z${p.index}`
  return (
    <li className={styles.zone} data-off={!p.enabled}>
      <div className={styles.zoneHead}>
        <span className={styles.zoneName}>{t.edit.zoneHead(p.index, p.distanceM)}</span>
        <Switch
          checked={p.enabled}
          changed={p.enabled !== p.enabledBase}
          label={t.edit.zoneSwitch(p.index)}
          disabled={p.disabled}
          onChange={p.onEnabled}
        />
      </div>
      <ThresholdMeter
        label={`${name} ${t.live.trigger}`}
        value={p.trigger.value}
        base={p.trigger.base}
        live={p.live?.trigger ?? null}
        disabled={p.disabled}
        onChange={p.onTrigger}
      />
      <ThresholdMeter
        label={`${name} ${t.live.maintain}`}
        value={p.maintain.value}
        base={p.maintain.base}
        live={p.live?.maintain ?? null}
        disabled={p.disabled}
        onChange={p.onMaintain}
      />
    </li>
  )
}
