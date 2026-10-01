import { relativeTime, toMillis } from '../format'
import { useNow } from '../hooks/useNow'

export function RelativeTime({ value }: { value: number | string }) {
  const now = useNow()
  const ms = toMillis(value)
  const iso = Number.isNaN(ms) ? undefined : new Date(ms).toISOString()
  return (
    <time dateTime={iso} title={iso ? new Date(ms).toLocaleString('ko-KR') : undefined}>
      {relativeTime(value, now)}
    </time>
  )
}
