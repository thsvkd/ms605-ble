import { useSyncExternalStore } from 'react'

// One shared 30 s ticker for every relative time on screen (RelativeTime, status hints).
const listeners = new Set<() => void>()
let now = Date.now()
let timer: ReturnType<typeof setInterval> | null = null

function subscribe(cb: () => void) {
  listeners.add(cb)
  if (timer === null) {
    timer = setInterval(() => {
      now = Date.now()
      for (const l of listeners) l()
    }, 30_000)
  }
  return () => {
    listeners.delete(cb)
    if (listeners.size === 0 && timer !== null) {
      clearInterval(timer)
      timer = null
    }
  }
}

export function useNow(): number {
  return useSyncExternalStore(subscribe, () => now)
}
