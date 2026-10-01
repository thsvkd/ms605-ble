import { BellOff, HelpCircle } from 'lucide-react'
import { t } from '../../strings'
import styles from './edit.module.css'
import { Switch } from './Switch'

interface Props {
  value: boolean | null
  base: boolean | null
  disabled: boolean
  onChange: (v: boolean) => void
}

/** 방해 금지 (tag32). When the device did not answer the read, there is nothing to edit. */
export function DndSwitch({ value, base, disabled, onChange }: Props) {
  return (
    <section className={styles.section} aria-label={t.adv.dnd}>
      <div className={styles.sectionHead}>
        <h2 className={styles.sectionTitle}>
          <BellOff size={18} aria-hidden />
          {t.adv.dnd}
        </h2>
        {base !== null && value !== null && (
          <Switch checked={value} changed={value !== base} label={t.adv.dnd} disabled={disabled} onChange={onChange} />
        )}
      </div>
      {base === null ? (
        <p className={styles.warnLine}>
          <HelpCircle size={16} aria-hidden />
          {t.adv.dndUnknown}
        </p>
      ) : (
        <p className={styles.note}>{t.adv.dndNote}</p>
      )}
    </section>
  )
}
