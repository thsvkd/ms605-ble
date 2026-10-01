import type { ReactNode } from 'react'
import styles from './ui.module.css'

export interface ControlProps {
  id: string
  className: string | undefined
  'aria-invalid': boolean
  'aria-describedby': string | undefined
}

interface Props {
  id: string
  label: string
  error?: string | null
  children: (control: ControlProps) => ReactNode
}

/** label + control + error text, wired with aria-describedby (9.7). */
export function Field({ id, label, error, children }: Props) {
  const errorId = `${id}-error`
  return (
    <div className={styles.field}>
      <label htmlFor={id} className={styles.label}>
        {label}
      </label>
      {children({
        id,
        className: styles.control,
        'aria-invalid': Boolean(error),
        'aria-describedby': error ? errorId : undefined,
      })}
      {error && (
        <p id={errorId} className={styles.error} role="alert">
          {error}
        </p>
      )}
    </div>
  )
}
