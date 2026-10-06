import { useState } from 'react'
import { failureText } from '../api/client'
import { useStrings } from '../strings'
import { Button } from './Button'
import { Dialog } from './Dialog'
import styles from './ui.module.css'

interface Props {
  open: boolean
  title: string
  body: string
  confirmLabel: string
  danger?: boolean
  onConfirm: () => Promise<unknown>
  onClose: () => void
}

/** For the irreversible or battery/link-heavy actions only (10.1-5). */
export function ConfirmDialog({ open, title, body, confirmLabel, danger, onConfirm, onClose }: Props) {
  const t = useStrings()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<unknown>(null)

  const close = () => {
    setError(null)
    onClose()
  }

  const confirm = async () => {
    setBusy(true)
    setError(null)
    try {
      await onConfirm()
      close()
    } catch (e) {
      setError(e)
    } finally {
      setBusy(false)
    }
  }

  return (
    <Dialog open={open} title={title} onClose={close}>
      <p className={styles.dialogText}>{body}</p>
      {error !== null && (
        <p className={styles.error} role="alert">
          {failureText(error)}
        </p>
      )}
      <div className={styles.dialogActions}>
        <Button onClick={close}>{t.form.cancel}</Button>
        <Button variant={danger ? 'dangerSolid' : 'primary'} onClick={confirm} disabled={busy}>
          {confirmLabel}
        </Button>
      </div>
    </Dialog>
  )
}
