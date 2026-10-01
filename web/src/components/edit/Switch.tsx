import { t } from '../../strings'
import styles from './edit.module.css'

interface Props {
  checked: boolean
  onChange: (v: boolean) => void
  /** The accessible name (the visible text is 켜짐/꺼짐 or `text`). */
  label: string
  text?: string
  disabled?: boolean
  /** Differs from the device: underlined. */
  changed?: boolean
}

/** A role=switch button: the state is in words next to the knob, never colour alone. */
export function Switch({ checked, onChange, label, text, disabled, changed }: Props) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      className={styles.switch}
      data-changed={changed || undefined}
      disabled={disabled}
      onClick={() => onChange(!checked)}
    >
      <span className={styles.knob} aria-hidden />
      <span aria-hidden>{text ?? (checked ? t.edit.zoneOn : t.edit.zoneOff)}</span>
    </button>
  )
}
