import { History, Loader2, Undo2 } from 'lucide-react'
import { useEffect, useId, useState } from 'react'
import { failureText, getSnapshot, listSnapshots, previewRollback, rollback } from '../../api/client'
import type { ApplyJobView, DraftPreview, ProfileView, RollbackItem, SnapshotDetail, SnapshotView } from '../../api/types'
import { formatDateTime, sectionList } from '../../apply'
import { expectRevOf } from '../../draft'
import { toMillis } from '../../format'
import { sensorName, useStore } from '../../store/store'
import { t } from '../../strings'
import { Button } from '../Button'
import { RelativeTime } from '../RelativeTime'
import { DiffPreview } from './DiffPreview'
import styles from './edit.module.css'

interface PreviewProps {
  items: RollbackItem[]
  title: string
  onStarted: (job: ApplyJobView) => void
  onBack: () => void
  sheet?: boolean
}

/** previewRollback -> the server's diff -> rollback (202). Shared by the picker and the results' 되돌리기. */
export function RollbackPreview({ items, title, onStarted, onBack, sheet }: PreviewProps) {
  const sensors = useStore((s) => s.sensors)
  const [preview, setPreview] = useState<DraftPreview | null>(null)
  const [error, setError] = useState<string | null>(null)
  const key = JSON.stringify(items)

  useEffect(() => {
    let live = true
    setPreview(null)
    setError(null)
    previewRollback({ items: JSON.parse(key) as RollbackItem[], expect_rev: null }).then(
      (p) => live && setPreview(p),
      (e: unknown) => live && setError(failureText(e)),
    )
    return () => {
      live = false
    }
  }, [key])

  if (error)
    return (
      <div className={styles.diff}>
        <p className={styles.error} role="alert">
          {error}
        </p>
        <Button onClick={onBack}>{t.diff.back}</Button>
      </div>
    )
  if (!preview)
    return (
      <p className={styles.lockNote} role="status">
        <Loader2 size={16} className="spin" aria-hidden />
        {t.history.loading}
      </p>
    )
  const names = Object.fromEntries(items.map((i) => [i.device_id, sensorName(sensors[i.device_id], i.device_id)]))
  return (
    <DiffPreview
      preview={preview}
      names={names}
      title={title}
      applyLabel={t.rollback.go}
      sheet={sheet}
      onBack={onBack}
      onApply={async () => onStarted(await rollback({ items, expect_rev: expectRevOf(preview) }))}
    />
  )
}

function reasonText(reason: string): string {
  if (reason === 'apply') return t.rollback.beforeApply
  if (reason === 'rollback') return t.rollback.beforeRollback
  return reason
}

/** What the sensor had at that point, for the sections that write changed. */
function summary(profile: ProfileView, sections: string[]): string {
  const parts: string[] = []
  if (sections.includes('sensitivity')) parts.push(`${t.section.sensitivity} ${t.sens[profile.sensitivity] ?? profile.sensitivity}`)
  if (sections.includes('zone_enable')) parts.push(t.edit.zonesOn(profile.zone_enable.filter(Boolean).length))
  if (sections.includes('zone_thresholds'))
    parts.push(
      profile.zone_thresholds
        .slice(0, 2)
        .map((z, i) => `Z${i} ${z.trigger}/${z.maintain}`)
        .join(' · ') + ' …',
    )
  if (sections.includes('dnd') && profile.dnd !== null) parts.push(`${t.section.dnd} ${profile.dnd ? t.edit.zoneOn : t.edit.zoneOff}`)
  if (sections.some((s) => s.startsWith('subsensor'))) parts.push(sectionList(sections.filter((s) => s.startsWith('subsensor'))))
  return parts.join(' · ')
}

interface Props {
  deviceId: string
  connected: boolean
  locked: boolean
  onStarted: (job: ApplyJobView) => void
}

/** 설정 백업: every snapshot, newest first; pick one, see the diff back to it, roll back (15.9.13). */
export function RollbackPicker({ deviceId, connected, locked, onStarted }: Props) {
  const name = useId()
  const [list, setList] = useState<SnapshotView[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [picked, setPicked] = useState<string | null>(null)
  const [detail, setDetail] = useState<SnapshotDetail | null>(null)
  const [previewing, setPreviewing] = useState(false)
  const lastSnapshot = useStore((s) => s.sensors[deviceId]?.last_snapshot?.name ?? null)

  useEffect(() => {
    let live = true
    listSnapshots(deviceId).then(
      (r) => live && setList(r.snapshots),
      (e: unknown) => live && setError(failureText(e)),
    )
    return () => {
      live = false
    }
  }, [deviceId, lastSnapshot])

  useEffect(() => {
    if (picked === null) return
    let live = true
    setDetail(null)
    getSnapshot(deviceId, picked).then(
      (d) => live && setDetail(d),
      () => live && setDetail(null),
    )
    return () => {
      live = false
    }
  }, [deviceId, picked])

  const pickedView = list?.find((s) => s.name === picked)

  return (
    <section className={styles.section} aria-labelledby={`${name}-title`}>
      <h2 id={`${name}-title`} className={styles.sectionTitle}>
        <History size={18} aria-hidden />
        {t.history.backups}
      </h2>
      {error && (
        <p className={styles.error} role="alert">
          {error}
        </p>
      )}
      {list === null && !error && <p className={styles.note}>{t.history.loading}</p>}
      {list?.length === 0 && <p className={styles.note}>{t.rollback.empty}</p>}
      {list && list.length > 0 && !previewing && (
        <>
          <ul className={styles.list} aria-label={t.rollback.listLabel}>
            {list.map((s, i) => (
              <li key={s.name} className={styles.histRow} data-picked={s.name === picked}>
                <label className={styles.radioRow}>
                  <input
                    type="radio"
                    name={name}
                    value={s.name}
                    checked={s.name === picked}
                    onChange={() => setPicked(s.name)}
                  />
                  <span className={styles.histMain}>
                    <strong>
                      <RelativeTime value={s.taken_at} />
                    </strong>
                    <span className={styles.histMuted}>{formatDateTime(toMillis(s.taken_at))}</span>
                    <span>{reasonText(s.reason)}</span>
                    {i === 0 && <span className={`${styles.tag} ${styles.tagNew}`}>{t.rollback.latest}</span>}
                  </span>
                </label>
                <div className={`${styles.chips} ${styles.pick}`}>
                  {s.sections.map((sec) => (
                    <span key={sec} className={styles.tag}>
                      {sectionList([sec])}
                    </span>
                  ))}
                </div>
                {s.name === picked && detail?.snapshot.name === s.name && (
                  <p className={`${styles.note} ${styles.pick}`}>{summary(detail.profile, s.sections)}</p>
                )}
              </li>
            ))}
          </ul>
          <p className={styles.note}>{t.rollback.note}</p>
          {!connected && <p className={styles.note}>{t.edit.needConnection}</p>}
          <Button icon={Undo2} disabled={picked === null || !connected || locked} onClick={() => setPreviewing(true)}>
            {t.rollback.preview}
          </Button>
        </>
      )}
      {previewing && picked && pickedView && (
        <RollbackPreview
          items={[{ device_id: deviceId, snapshot: picked }]}
          title={t.rollback.title(formatDateTime(toMillis(pickedView.taken_at)))}
          onBack={() => setPreviewing(false)}
          onStarted={(job) => {
            setPreviewing(false)
            setPicked(null)
            onStarted(job)
          }}
        />
      )}
    </section>
  )
}
