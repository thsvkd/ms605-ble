import { useEffect, useRef, useState } from 'react'
import { type Store, useStore } from '../store/store'
import { t } from '../strings'

/** What changed between two store states that a screen-reader user should hear (9.7). */
export function announcements(prev: Store, next: Store): string[] {
  // a fresh snapshot (first load, reconnect) is not news
  if (prev.conn !== 'open' || (prev.sensors === next.sensors && prev.gather === next.gather)) return []
  const out: string[] = []
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
