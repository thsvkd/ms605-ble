import type { CalibrationJobView } from '../../api/types'
import { compareCell } from '../../calibration'
import { t } from '../../strings'
import styles from './calibrate.module.css'

function Cell({ before, after, label }: { before: number; after: number; label: string }) {
  const d = after - before
  return (
    <td data-label={label}>
      <span className={styles.delta} data-dir={d > 0 ? 'up' : d < 0 ? 'down' : 'same'}>
        {compareCell(before, after)}
      </span>
    </td>
  )
}

/** Thresholds per zone before and after: `70 → 64 (−6)`. */
export function ThresholdCompare({ job, alias }: { job: CalibrationJobView; alias: string }) {
  const { before, after } = job
  if (!before || !after) return <p className={styles.muted}>{t.compare.none}</p>
  return (
    <table className={styles.compare}>
      <caption>{t.compare.title(alias)}</caption>
      <thead>
        <tr>
          <th scope="col">{t.compare.zone}</th>
          <th scope="col">{t.live.trigger}</th>
          <th scope="col">{t.live.maintain}</th>
        </tr>
      </thead>
      <tbody>
        {before.map((b, i) => {
          const a = after[i]
          if (!a) return null
          return (
            <tr key={i}>
              <th scope="row">Z{i}</th>
              <Cell before={b.trigger} after={a.trigger} label={t.live.trigger} />
              <Cell before={b.maintain} after={a.maintain} label={t.live.maintain} />
            </tr>
          )
        })}
      </tbody>
    </table>
  )
}
