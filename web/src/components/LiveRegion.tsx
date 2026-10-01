import { useEffect, useRef, useState } from 'react'
import { kindText } from '../apply'
import { batchTotals } from '../calibration'
import { selectApplyTally, sensorName, type Store, useStore } from '../store/store'
import { t } from '../strings'

/** Batch news (14.8.10): the round starts running, the batch ends, a sensor drops mid-calibration. */
function batchAnnouncements(prev: Store, next: Store): string[] {
  const a = prev.batch
  const b = next.batch
  if (!b || a === b) return []
  const out: string[] = []
  const same = a?.batch_id === b.batch_id
  if (b.state === 'running' && (!same || a?.state !== 'running' || a.round !== b.round))
    out.push(t.announce.batchStarted)
  const before = new Map((same ? (a?.jobs ?? []) : []).map((j) => [j.device_id, j.state]))
  for (const j of b.jobs) {
    if (j.state === 'lost' && before.get(j.device_id) !== 'lost')
      out.push(t.announce.jobLost(sensorName(next.sensors[j.device_id], j.device_id)))
  }
  if (b.state === 'done' && !(same && a?.state === 'done' && a.round === b.round)) {
    const all = batchTotals(b)
    out.push(t.announce.batchDone(all.succeeded, all.failed))
  }
  return out
}

/** Apply news (15.9.12): a job starts, a job ends (verified and failed counts). */
function applyAnnouncements(prev: Store, next: Store): string[] {
  const a = prev.apply
  const b = next.apply
  if (!b || a === b) return []
  const same = a?.apply_id === b.apply_id
  const kind = kindText(b.kind)
  if (!same && b.state === 'running') return [t.announce.applyStarted(kind)]
  if (b.state === 'done' && !(same && a?.state === 'done')) {
    const n = selectApplyTally(b)
    return [t.announce.applyDone(kind, n.verified, n.failed)]
  }
  return []
}

/** What changed between two store states that a screen-reader user should hear (9.7). */
export function announcements(prev: Store, next: Store): string[] {
  // a fresh snapshot (first load, reconnect) is not news
  if (prev.conn !== 'open') return []
  const out = [...batchAnnouncements(prev, next), ...applyAnnouncements(prev, next)]
  if (prev.sensors === next.sensors && prev.gather === next.gather) return out
  for (const [id, s] of Object.entries(next.sensors)) {
    const before = prev.sensors[id]
    const was = before?.live?.link
    const now = s.live?.link
    if (now === was) continue
    if (now === 'connected') {
      if (s.registry) out.push(t.sensor.announceConnected(s.registry.alias))
      else out.push(t.sensor.announceNew(s.live?.name ?? s.live?.address ?? id))
    } else if (now === 'lost') {
      out.push(t.sensor.announceLost(s.registry?.alias ?? s.live?.name ?? id))
    }
  }
  if (prev.gather.gathering !== next.gather.gathering) {
    out.push(next.gather.gathering ? t.gather.started : t.gather.stopped)
  }
  return out
}

export function LiveRegion() {
  const [text, setText] = useState('')
  const last = useRef<{ text: string; at: number }>({ text: '', at: 0 })

  useEffect(
    () =>
      useStore.subscribe((next, prev) => {
        const lines = announcements(prev, next)
        if (lines.length === 0) return
        const msg = lines.join('. ')
        const now = Date.now()
        if (msg === last.current.text && now - last.current.at < 1000) return
        last.current = { text: msg, at: now }
        setText(msg)
      }),
    [],
  )

  return (
    <div className="visually-hidden" aria-live="polite" aria-atomic="true">
      {text}
    </div>
  )
}
