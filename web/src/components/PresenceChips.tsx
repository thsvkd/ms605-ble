import { Eye, EyeOff, UserRound, UserRoundX } from 'lucide-react'
import type { LiveData } from '../api/types'
import { t } from '../strings'
import styles from './live.module.css'

/** The CLI header line: PIR and each sub-sensor's own presence call. */
export function PresenceChips({ frame }: { frame: LiveData }) {
  const pir = frame.pir
  return (
    <ul className={styles.chips} aria-label={t.live.presenceLabel}>
      <li className={styles.chip} data-kind={pir ? 'danger' : 'off'}>
        {pir ? <Eye size={14} aria-hidden /> : <EyeOff size={14} aria-hidden />}
        {pir === null ? t.live.pirUnknown : pir ? t.live.pirOn : t.live.pirOff}
      </li>
      {frame.sub_sensor_presence.map((present, i) => (
        <li key={i} className={styles.chip} data-kind={present ? 'ok' : 'off'}>
          {present ? <UserRound size={14} aria-hidden /> : <UserRoundX size={14} aria-hidden />}
          {present ? t.live.subOn(i + 1) : t.live.subOff(i + 1)}
        </li>
      ))}
    </ul>
  )
}
