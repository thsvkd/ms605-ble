import type { SensorStatus } from '../status'
import { useLocale } from '../strings'
import styles from './ui.module.css'

/** Icon + text + colour; the icon is decorative, the text carries the meaning (9.7). */
export function StatusBadge({ status }: { status: SensorStatus }) {
  useLocale()
  const Icon = status.icon
  return (
    <span className={`${styles.badge} ${styles[status.kind]}`} data-kind={status.kind}>
      {status.kind === 'ok' ? (
        <span className={styles.pulse} aria-hidden />
      ) : (
        <Icon size={16} aria-hidden className={status.spin ? 'spin' : undefined} />
      )}
      {status.label}
    </span>
  )
}
