import { Battery, BatteryLow } from 'lucide-react'
import type { SensorView } from '../api/types'
import { t } from '../strings'
import styles from './ui.module.css'

/** The live reading when connected, else the registry's last known value marked "(마지막 값)". */
export function BatteryIndicator({ sensor }: { sensor: SensorView }) {
  const live = sensor.live?.link === 'connected' ? sensor.live.battery_pct : null
  const pct = live ?? sensor.live?.battery_pct ?? sensor.registry?.battery_pct ?? null
  if (pct === null) return null
  const isLast = live === null
  const low = pct <= 20
  const Icon = low ? BatteryLow : Battery
  return (
    <span className={`${styles.battery} ${low ? styles.batteryLow : ''}`}>
      <Icon size={16} aria-hidden />
      {isLast ? t.batteryLast(pct) : t.battery(pct)}
    </span>
  )
}
