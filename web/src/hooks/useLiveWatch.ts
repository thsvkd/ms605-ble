import { useEffect } from 'react'
import { useStore } from '../store/store'

/**
 * Watch these sensors' live frames while the calling component is mounted (14.8.3). Ref-counted in
 * the store, so two views of one sensor make one server subscription; unmounting (route change) unwatches.
 */
export function useLiveWatch(ids: readonly string[]): void {
  const key = [...new Set(ids)].sort().join(',')
  useEffect(() => {
    const list = key ? key.split(',') : []
    const { watchLive, unwatchLive } = useStore.getState()
    watchLive(list)
    return () => unwatchLive(list)
  }, [key])
}
