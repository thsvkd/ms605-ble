import { AlertTriangle, X } from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'
import type { Notice } from '../api/types'
import { useStore } from '../store/store'
import { t } from '../strings'
import styles from './shell.module.css'

const VISIBLE_MS = 8000

function noticeText(n: Notice): string {
  if (n.code === 'gather_failed') return t.notice.gatherFailed(n.name ?? n.address ?? n.device_id ?? '')
  return n.message
}

/** Short-lived toasts for server notices that arrive while this page is open. */
export function Notices() {
  const notices = useStore((s) => s.notices)
  const [shown, setShown] = useState<{ key: string; notice: Notice }[]>([])
  const [seen] = useState(() => new Set(notices)) // notices from before this page loaded stay quiet

  useEffect(() => {
    const fresh = notices.filter((n) => !seen.has(n))
    if (fresh.length === 0) return
    for (const n of fresh) seen.add(n)
    const added = fresh.map((notice) => ({ key: `${notice.at}-${notice.code}-${notice.address}`, notice }))
    setShown((cur) => [...cur, ...added].slice(-3))
  }, [notices, seen])

  const dismiss = useCallback((key: string) => setShown((cur) => cur.filter((x) => x.key !== key)), [])

  if (shown.length === 0) return null
  return (
    <div className={styles.notices} role="status">
      {shown.map(({ key, notice }) => (
        <Toast key={key} id={key} notice={notice} onDismiss={dismiss} />
      ))}
    </div>
  )
}

/** One toast with its own timer: a newer toast never restarts or cancels an older one's. */
function Toast({ id, notice, onDismiss }: { id: string; notice: Notice; onDismiss: (key: string) => void }) {
  useEffect(() => {
    const timer = setTimeout(() => onDismiss(id), VISIBLE_MS)
    return () => clearTimeout(timer)
  }, [id, onDismiss])
  return (
    <div className={`${styles.notice} ${notice.level === 'error' ? styles.noticeError : ''}`}>
      <AlertTriangle size={16} className={styles.noticeIcon} aria-hidden />
      <span className={styles.noticeText}>{noticeText(notice)}</span>
      <button type="button" className={styles.noticeClose} aria-label={t.form.close} onClick={() => onDismiss(id)}>
        <X size={16} aria-hidden />
      </button>
    </div>
  )
}
