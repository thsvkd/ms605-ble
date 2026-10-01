import { Bluetooth } from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import { Link, useSearch } from 'wouter'
import { buttonClass } from '../components/Button'
import { SensorLiveCard } from '../components/SensorLiveCard'
import { SensorPicker } from '../components/SensorPicker'
import styles from '../components/live.module.css'
import { useLiveWatch } from '../hooks/useLiveWatch'
import { selectSessions, useStore } from '../store/store'
import { t } from '../strings'

const SAVED_KEY = 'ms605.monitorIds'

function readSaved(): string[] | null {
  try {
    const raw = localStorage.getItem(SAVED_KEY)
    const parsed: unknown = raw === null ? null : JSON.parse(raw)
    return Array.isArray(parsed) ? parsed.filter((x): x is string => typeof x === 'string') : null
  } catch {
    return null
  }
}

function writeSaved(ids: string[]): void {
  try {
    localStorage.setItem(SAVED_KEY, JSON.stringify(ids))
  } catch {
    // convenience only
  }
}

/** The server's DeviceId pattern (14.4): one id that fails it makes the server drop the whole frame. */
const DEVICE_ID = /^[0-9a-f]{2,128}$/

/** `?ids=a,b` (comma separated device ids), as 14.8.1. A hand-typed URL: case folded, malformed ids dropped. */
export function idsParam(search: string): string[] | null {
  const raw = new URLSearchParams(search).get('ids')
  if (!raw) return null
  const ids = raw
    .split(',')
    .map((s) => s.trim().toLowerCase())
    .filter((s) => DEVICE_ID.test(s))
  return ids.length ? [...new Set(ids)] : null
}

export function MonitorScreen() {
  const sensors = useStore((s) => s.sensors)
  const sites = useStore((s) => s.sites)
  const synced = useStore((s) => s.lastSeq !== null)
  const search = useSearch()
  const sessions = useMemo(() => selectSessions({ sensors, sites }), [sensors, sites])
  const [selected, setSelected] = useState<string[] | null>(null)

  // The first choice waits for the snapshot: ?ids= -> the saved choice (sessions only) -> every connected sensor.
  useEffect(() => {
    if (selected !== null || !synced) return
    const have = new Set(sessions.map((s) => s.device_id))
    const fromUrl = idsParam(search)
    const saved = readSaved()?.filter((id) => have.has(id))
    setSelected(
      fromUrl ?? (saved?.length ? saved : sessions.filter((s) => s.live?.link === 'connected').map((s) => s.device_id)),
    )
  }, [selected, synced, sessions, search])

  const choose = (ids: string[]) => {
    setSelected(ids)
    writeSaved(ids)
  }

  useLiveWatch(selected ?? [])

  const shown = useMemo(() => {
    const order = new Map(sessions.map((s, i) => [s.device_id, i]))
    return (selected ?? []).filter((id) => order.has(id)).sort((a, b) => (order.get(a) ?? 0) - (order.get(b) ?? 0))
  }, [selected, sessions])

  return (
    <div className={styles.screen}>
      <h1 className={styles.title}>{t.monitor.title}</h1>
      {sessions.length === 0 ? (
        <div className={styles.emptyState}>
          <p className={styles.emptyTitle}>{t.monitor.empty}</p>
          <p>{t.monitor.emptyHint}</p>
          <Link href="/gather" className={buttonClass('primary')}>
            <Bluetooth size={18} aria-hidden />
            {t.nav.gather}
          </Link>
        </div>
      ) : (
        <>
          <div className={styles.top}>
            <SensorPicker sensors={sessions} selected={selected ?? []} onChange={choose} />
          </div>
          <p className={styles.legend}>
            <span>{t.monitor.legend}</span>
            <span>{t.monitor.legendTrigger}</span>
          </p>
          {shown.length === 0 ? (
            <p className={styles.emptyNote}>{t.monitor.noneSelected}</p>
          ) : (
            <div className={styles.cards}>
              {shown.map((id) => (
                <SensorLiveCard key={id} deviceId={id} />
              ))}
            </div>
          )}
        </>
      )}
    </div>
  )
}
