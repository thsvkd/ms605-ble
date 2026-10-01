import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { SensorView } from '../api/types'
import { selectGathered, useStore } from '../store/store'
import { t } from '../strings'
import { GatheredItem } from './GatheredItem'
import styles from './gather.module.css'

function newestUnnamed(list: SensorView[]): string | null {
  return list.find((s) => s.registry === null)?.device_id ?? null
}

function formBusy(dirty: Set<string>): boolean {
  if (dirty.size > 0) return true
  return document.activeElement?.closest('[data-name-form]') != null
}

/**
 * Sensors with a session, newest first. When a new sensor arrives, only the newest unnamed one is
 * opened — and nothing moves while some form has focus or unsaved input (9.4). Never steals focus.
 */
export function GatheredList() {
  const sensors = useStore((s) => s.sensors)
  const synced = useStore((s) => s.lastSeq !== null)
  const list = useMemo(() => selectGathered({ sensors }), [sensors])
  // The baseline is what the first snapshot held: those are not "arrivals" and are not highlighted.
  const initialIds = useRef<Set<string> | null>(null)
  if (initialIds.current === null && synced) initialIds.current = new Set(list.map((s) => s.device_id))
  const seen = useRef<Set<string> | null>(null)
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set())
  const [focusId, setFocusId] = useState<string | null>(null)
  const dirty = useRef(new Set<string>())

  useEffect(() => {
    if (initialIds.current === null) return
    if (seen.current === null) {
      // first synced render: open the newest unnamed sensor, if any
      seen.current = new Set(initialIds.current)
      const id = newestUnnamed(list)
      if (id) setExpanded(new Set([id]))
      return
    }
    const known = seen.current
    const arrivals = list.filter((s) => !known.has(s.device_id))
    if (arrivals.length === 0) return
    for (const s of arrivals) known.add(s.device_id)
    const target = newestUnnamed(arrivals)
    if (target === null || formBusy(dirty.current)) return
    setExpanded(new Set([target]))
    setFocusId(null)
  }, [list])

  const onExpand = useCallback((id: string, focus: boolean) => {
    setExpanded((cur) => new Set(cur).add(id))
    if (focus) setFocusId(id)
  }, [])
  const onCollapse = useCallback((id: string) => {
    setExpanded((cur) => {
      const next = new Set(cur)
      next.delete(id)
      return next
    })
  }, [])
  const onDirtyChange = useCallback((id: string, isDirty: boolean) => {
    if (isDirty) dirty.current.add(id)
    else dirty.current.delete(id)
  }, [])

  if (list.length === 0) return null
  return (
    <section className={styles.listArea} aria-labelledby="gathered-title">
      <h2 id="gathered-title" className={styles.listTitle}>
        {t.gather.listTitle(list.length)}
      </h2>
      <ul className={styles.list}>
        {list.map((s) => (
          <GatheredItem
            key={s.device_id}
            deviceId={s.device_id}
            arrived={initialIds.current !== null && !initialIds.current.has(s.device_id)}
            expanded={expanded.has(s.device_id)}
            focusName={focusId === s.device_id}
            onExpand={onExpand}
            onCollapse={onCollapse}
            onDirtyChange={onDirtyChange}
          />
        ))}
      </ul>
    </section>
  )
}
