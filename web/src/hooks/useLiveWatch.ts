import { useEffect, useRef } from 'react'
import { useStore } from '../store/store'
import { usePageVisible } from './usePageVisible'

/**
 * Watch these sensors' live frames while the calling component is mounted and the tab is visible (14.8.3,
 * 14.8.5.1). Ref-counted in the store, so two views of one sensor make one server subscription.
 *
 * A change of `ids` applies only the difference, adding before removing: an id in both lists never drops to
 * 0, so its frame stays and the server sees no unsubscribe/subscribe pair (one sensor connecting must not
 * blink every other card). A hidden tab empties the list; unmounting (route change) unwatches all.
 */
export function useLiveWatch(ids: readonly string[]): void {
  const visible = usePageVisible()
  const key = visible ? [...new Set(ids)].sort().join(',') : ''
  const held = useRef<string[]>([])
  useEffect(() => {
    const next = key ? key.split(',') : []
    const before = new Set(held.current)
    const after = new Set(next)
    const { watchLive, unwatchLive } = useStore.getState()
    watchLive(next.filter((id) => !before.has(id)))
    unwatchLive(held.current.filter((id) => !after.has(id)))
    held.current = next
  }, [key])
  useEffect(
    () => () => {
      useStore.getState().unwatchLive(held.current)
      held.current = [] // StrictMode runs the effects again on the same instance: start from nothing
    },
    [],
  )
}
