import { ChevronRight, ClipboardList } from 'lucide-react'
import { useEffect, useState } from 'react'
import { calibrationHistory, failureText } from '../../api/client'
import type { CalibrationRecord } from '../../api/types'
import { formatDateTime } from '../../apply'
import { toMillis } from '../../format'
import { useStrings } from '../../strings'
import cal from '../calibrate/calibrate.module.css'
import { RelativeTime } from '../RelativeTime'
import styles from './edit.module.css'

/** 보정 기록 from storage (calibration_history.jsonl), newest first; no connection needed. */
export function CalibrationHistoryList({ deviceId, refresh }: { deviceId: string; refresh?: unknown }) {
  const t = useStrings()
  const [records, setRecords] = useState<CalibrationRecord[] | null>(null)
  const [error, setError] = useState<{ cause: unknown } | null>(null)

  useEffect(() => {
    let live = true
    calibrationHistory(deviceId).then(
      (r) => live && setRecords(r.records),
      (e: unknown) => live && setError({ cause: e }),
    )
    return () => {
      live = false
    }
  }, [deviceId, refresh])

  return (
    <section className={styles.section} aria-label={t.history.calibration}>
      <h2 className={styles.sectionTitle}>
        <ClipboardList size={18} aria-hidden />
        {t.history.calibration}
      </h2>
      {error && (
        <p className={styles.error} role="alert">
          {failureText(error.cause)}
        </p>
      )}
      {records === null && !error && <p className={styles.note}>{t.history.loading}</p>}
      {records?.length === 0 && <p className={styles.note}>{t.history.empty}</p>}
      {records && records.length > 0 && (
        <ul className={styles.list}>
          {records.map((r, i) => (
            <li key={`${r.timestamp}-${i}`} className={styles.histRow}>
              <details className={cal.details}>
                <summary>
                  <ChevronRight size={16} className={cal.chevron} aria-hidden />
                  <span className={styles.histMain}>
                    <strong>
                      <RelativeTime value={r.timestamp} />
                    </strong>
                    <span className={styles.histMuted}>{formatDateTime(toMillis(r.timestamp))}</span>
                    {r.sensitivity !== null && <span>{t.sens[r.sensitivity] ?? r.sensitivity}</span>}
                    {r.detect_mode !== null && <span className={styles.histMuted}>{t.mode[r.detect_mode] ?? r.detect_mode}</span>}
                  </span>
                </summary>
                <table className={cal.compare}>
                  <caption>{t.history.zones}</caption>
                  <thead>
                    <tr>
                      <th scope="col">{t.history.zone}</th>
                      <th scope="col">{t.history.distance}</th>
                      <th scope="col">{t.live.trigger}</th>
                      <th scope="col">{t.live.maintain}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {r.zones.map((z) => (
                      <tr key={z.index}>
                        <th scope="row">Z{z.index}</th>
                        <td data-label={t.history.distance}>{z.distance_m === null ? '—' : `${z.distance_m.toFixed(1)} m`}</td>
                        <td data-label={t.live.trigger}>{z.trigger}</td>
                        <td data-label={t.live.maintain}>{z.maintain}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </details>
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}
