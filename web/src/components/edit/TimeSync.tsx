import { Clock } from 'lucide-react'
import { useState } from 'react'
import { failureText, timeSync } from '../../api/client'
import { t } from '../../strings'
import { Button } from '../Button'
import styles from './edit.module.css'

function hhmmss(epochS: number): string {
  return new Date(epochS * 1000).toLocaleTimeString('ko-KR', { hour12: false })
}

/** An action, not a draft (G34): writes this computer's time to the sensor; nothing to roll back, no confirm. */
export function TimeSync({ deviceId, disabled }: { deviceId: string; disabled: boolean }) {
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState<{ ok: boolean; text: string } | null>(null)

  const run = async () => {
    setBusy(true)
    setResult(null)
    try {
      const item = (await timeSync([deviceId])).items[0]
      if (item?.written_at != null) setResult({ ok: true, text: t.timeSync.done(hhmmss(item.written_at)) })
      else setResult({ ok: false, text: t.timeSync.failed(item?.error ?? '') })
    } catch (e) {
      setResult({ ok: false, text: t.timeSync.failed(failureText(e)) })
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
            {result.text}
          </p>
        )}
      </div>
    </section>
  )
}
