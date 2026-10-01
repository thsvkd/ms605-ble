import { useEffect, useState } from 'react'
import { useStore } from '../store/store'

/** Seconds left on the server's countdown, redrawn every 250 ms between its 1 Hz messages; null when none. */
export function useCountdown(): number | null {
  const countdown = useStore((s) => s.countdown)
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (!countdown) return
    setNow(Date.now())
    const timer = setInterval(() => setNow(Date.now()), 250)
    return () => clearInterval(timer)
  }, [countdown])
  if (!countdown) return null
  return Math.max(0, countdown.remaining_s - (now - countdown.received_at) / 1000)
}
