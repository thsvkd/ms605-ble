import { Clock } from 'lucide-react'
import { useState } from 'react'
import { failureText, timeSync } from '../../api/client'
import { useLocale, useStrings } from '../../strings'
import { Button } from '../Button'
import styles from './edit.module.css'

function hhmmss(epochS: number, locale: 'ko' | 'en'): string {
  return new Date(epochS * 1000).toLocaleTimeString(locale === 'ko' ? 'ko-KR' : 'en-US', { hour12: false })
}

/** An action, not a draft (G34): writes this computer's time to the sensor; nothing to roll back, no confirm. */
export function TimeSync({ deviceId, disabled }: { deviceId: string; disabled: boolean }) {
  const t = useStrings()
  const locale = useLocale()
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState<
    { ok: true; writtenAt: number } | { ok: false; detail: string } | { ok: false; cause: unknown } | null
  >(null)

  const run = async () => {
    setBusy(true)
    setResult(null)
    try {
      const item = (await timeSync([deviceId])).items[0]
      if (item?.written_at != null) setResult({ ok: true, writtenAt: item.written_at })
      else setResult({ ok: false, detail: item?.error ?? '' })
    } catch (e) {
      setResult({ ok: false, cause: e })
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className={styles.section} aria-label={t.timeSync.title}>
      <h2 className={styles.sectionTitle}>
        <Clock size={18} aria-hidden />
        {t.timeSync.title}
      </h2>
      <p className={styles.note}>{t.timeSync.body}</p>
      <div className={styles.timeSync}>
        <Button icon={Clock} iconSpin={busy} onClick={run} disabled={busy || disabled}>
          {t.timeSync.action}
        </Button>
        {result && (
          <p className={result.ok ? styles.ok : styles.error} role="status">
            {result.ok
              ? t.timeSync.done(hhmmss(result.writtenAt, locale))
              : t.timeSync.failed('cause' in result ? failureText(result.cause) : result.detail)}
          </p>
        )}
      </div>
    </section>
  )
}
