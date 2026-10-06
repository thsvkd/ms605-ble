import { useCallback, useMemo, useRef, useState } from 'react'
import { selectGathered, useStore } from '../store/store'
import { useStrings } from '../strings'
import { GatheredItem } from './GatheredItem'
import styles from './gather.module.css'

/** Sensors with a session, newest first. New unregistered sensors open ready for registration. */
export function GatheredList() {
  const t = useStrings()
  const sensors = useStore((s) => s.sensors)
  const synced = useStore((s) => s.lastSeq !== null)
  const list = useMemo(() => selectGathered({ sensors }), [sensors])
  // The baseline is what the first snapshot held: those are not "arrivals" and are not highlighted.
  const initialIds = useRef<Set<string> | null>(null)
  if (initialIds.current === null && synced) initialIds.current = new Set(list.map((s) => s.device_id))
  const [collapsed, setCollapsed] = useState<Set<string>>(() => new Set())
  const [focusId, setFocusId] = useState<string | null>(null)

  const onExpand = useCallback((id: string, focus: boolean) => {
    setCollapsed((cur) => {
      const next = new Set(cur)
      next.delete(id)
      return next
    })
    if (focus) setFocusId(id)
  }, [])
  const onCollapse = useCallback((id: string) => {
    setCollapsed((cur) => new Set(cur).add(id))
    setFocusId((current) => current === id ? null : current)
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
            expanded={!s.registry && !collapsed.has(s.device_id)}
            focusName={focusId === s.device_id}
            onExpand={onExpand}
            onCollapse={onCollapse}
          />
        ))}
      </ul>
    </section>
  )
}
