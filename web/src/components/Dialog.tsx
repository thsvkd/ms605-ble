import { type ReactNode, useEffect, useId, useRef } from 'react'
import styles from './ui.module.css'

interface Props {
  open: boolean
  title: string
  onClose: () => void
  children: ReactNode
}

/** Native <dialog> (focus trap, Esc) shown as a bottom sheet on phones. */
export function Dialog({ open, title, onClose, children }: Props) {
  const ref = useRef<HTMLDialogElement>(null)
  const titleId = useId()

  useEffect(() => {
    const el = ref.current
    if (!el) return
    if (open && !el.open) el.showModal()
    else if (!open && el.open) el.close()
  }, [open])

  return (
    <dialog
      ref={ref}
      className={styles.dialog}
      aria-labelledby={titleId}
      onClose={onClose}
      onCancel={(e) => {
        e.preventDefault()
        onClose()
      }}
      onClick={(e) => {
        if (e.target === ref.current) onClose() // backdrop
      }}
    >
      {open && (
        <div className={styles.dialogBody}>
          <h2 id={titleId} className={styles.dialogTitle}>
            {title}
          </h2>
          {children}
        </div>
      )}
    </dialog>
  )
}
