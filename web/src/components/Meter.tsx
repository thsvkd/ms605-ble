import { meter } from '../meter'
import { useStrings } from '../strings'
import styles from './live.module.css'

interface Props {
  value: number
  threshold: number
  /** 재실 트리거 / 재실 유지 */
  label: string
  /** Colours the number: the device's trigger flag, or maintain > threshold (as the CLI). */
  hot: boolean
  /** No label, 8 px track (sensor detail strip). */
  compact?: boolean
}

/** The CLI meter as a 16-cell grid: the tick never moves; the fill crosses it, in red, iff value > threshold. */
export function Meter({ value, threshold, label, hot, compact }: Props) {
  const t = useStrings()
  const { cells, over } = meter(value, threshold)
  return (
    <div className={`${styles.meter} ${compact ? styles.compact : ''}`} data-over={over}>
      <span className={styles.meterLabel}>{label}</span>
      <div className={styles.track} role="img" aria-label={t.live.meterAria(label, value, threshold, over)}>
        {cells.map((c, i) => (
          <span key={i} className={styles.cell} data-cell={c} />
        ))}
      </div>
      <span className={styles.value} data-hot={hot}>
        {value}/{threshold}
      </span>
    </div>
  )
}
