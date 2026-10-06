import { ChevronRight, Download, FlaskConical } from 'lucide-react'
import { useId, useState } from 'react'
import { deviceHistory, failureText } from '../../api/client'
import type { DeviceHistory, DeviceHistoryKind } from '../../api/types'
import { formatDateTime } from '../../apply'
import { useStrings } from '../../strings'
import { Button } from '../Button'
import cal from '../calibrate/calibrate.module.css'
import styles from './edit.module.css'

const KINDS: DeviceHistoryKind[] = ['presence', 'light']

/** 기기 기록 — 실험적 (G35, SPEC 8.8): one read, no paging, the record layout unverified; always labelled. */
export function DeviceHistoryPanel({ deviceId, connected }: { deviceId: string; connected: boolean }) {
  const t = useStrings()
  const name = useId()
  const [kind, setKind] = useState<DeviceHistoryKind>('presence')
  const [detail, setDetail] = useState(false)
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState<DeviceHistory | null>(null)
  const [error, setError] = useState<{ cause: unknown } | null>(null)

  const read = async () => {
    setBusy(true)
    setError(null)
    setResult(null)
    try {
      setResult(await deviceHistory(deviceId, kind, kind === 'presence' && detail))
    } catch (e) {
      setError({ cause: e })
    } finally {
      setBusy(false)
    }
  }

  const empty = result && (result.kind === 'presence' ? result.presence.length === 0 : result.light.length === 0)

  return (
    <details className={`${styles.section} ${cal.details}`}>
      <summary>
        <ChevronRight size={16} className={cal.chevron} aria-hidden />
        <FlaskConical size={16} aria-hidden />
        {t.history.device}
        <span className={styles.badgeExp}>{t.history.experimental}</span>
      </summary>
      <p className={styles.note}>{t.history.deviceNote}</p>
      <fieldset className={cal.segmented}>
        <legend className="visually-hidden">{t.history.device}</legend>
        {KINDS.map((k) => (
          <label key={k} className={cal.segment}>
            <input type="radio" name={name} value={k} checked={kind === k} onChange={() => setKind(k)} />
            {k === 'presence' ? t.history.kindPresence : t.history.kindLight}
          </label>
        ))}
      </fieldset>
      {kind === 'presence' && (
        <label className={cal.check}>
          <input type="checkbox" checked={detail} onChange={(e) => setDetail(e.target.checked)} />
          <span>{t.history.detail}</span>
        </label>
      )}
      <Button icon={Download} onClick={read} disabled={!connected || busy}>
        {t.history.read}
      </Button>
      {!connected && <p className={styles.note}>{t.edit.needConnection}</p>}
      {error && (
        <p className={styles.error} role="alert">
          {failureText(error.cause)}
        </p>
      )}
      {empty && <p className={styles.note}>{t.history.deviceEmpty}</p>}
      {result && !empty && (
        <>
          <p className={styles.note}>{t.history.deviceClock}</p>
          <ul className={styles.list}>
            {result.kind === 'presence'
              ? result.presence.map((r) => {
                  const zones = r.zone_presence.flatMap((p, z) => (p ? [`Z${z}`] : []))
                  return (
                    <li key={r.index} className={styles.histRow}>
                      <span className={styles.histMain}>
                        <strong>{formatDateTime(r.timestamp * 1000)}</strong>
                        {r.sensor_presence.flatMap((p, i) => (p ? [<span key={i}>{t.history.subPresent(i + 1)}</span>] : []))}
                        <span className={styles.histMuted}>
                          {zones.length ? t.history.zonesPresent(zones.join(', ')) : t.history.noZonePresent}
                        </span>
                      </span>
                    </li>
                  )
                })
              : result.light.map((r) => (
                  <li key={r.index} className={styles.histRow}>
                    <span className={styles.histMain}>
                      <strong>{formatDateTime(r.timestamp * 1000)}</strong>
                      <span>{t.history.lux(r.light_lux)}</span>
                    </span>
                  </li>
                ))}
          </ul>
        </>
      )}
    </details>
  )
}
