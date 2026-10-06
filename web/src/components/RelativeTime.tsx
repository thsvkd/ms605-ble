import { relativeTime, toMillis } from '../format'
import { useNow } from '../hooks/useNow'
import { useLocale } from '../strings'

export function RelativeTime({ value }: { value: number | string }) {
  const locale = useLocale()
  const now = useNow()
  const ms = toMillis(value)
  const iso = Number.isNaN(ms) ? undefined : new Date(ms).toISOString()
  return (
    <time dateTime={iso} title={iso ? new Date(ms).toLocaleString(locale === 'ko' ? 'ko-KR' : 'en-US') : undefined}>
      {relativeTime(value, now)}
    </time>
  )
}
