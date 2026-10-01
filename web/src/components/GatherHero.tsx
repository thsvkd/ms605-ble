import { Bluetooth, BluetoothSearching } from 'lucide-react'
import { useStore } from '../store/store'
import { t } from '../strings'
import { ConnectingLine } from './ConnectingLine'
import styles from './gather.module.css'

/** The three states of 10.3. `compact` is the phone's one-liner once a sensor has been gathered. */
export function GatherHero({ compact, wide }: { compact: boolean; wide: boolean }) {
  const gathering = useStore((s) => s.gather.gathering)
  const connecting = useStore((s) => s.gather.connecting.length)

  if (gathering && compact) {
    return (
      <div className={styles.compact} role="status">
        <BluetoothSearching size={22} className={styles.compactIcon} aria-hidden />
        <span className={styles.compactText}>
          <span>{t.gather.pressTitle}</span>
          {connecting > 0 && <span className={styles.compactSub}>· {t.gather.connecting(connecting)}</span>}
        </span>
      </div>
    )
  }

  return (
    <section className={styles.hero} data-state={gathering ? 'press' : 'idle'} aria-live="polite">
      <div className={styles.sonar} aria-hidden>
        {gathering && (
          <>
            <span className={styles.ring} />
            <span className={styles.ring} />
            <span className={styles.ring} />
          </>
        )}
        <span className={styles.sonarCore}>{gathering ? <BluetoothSearching size={34} /> : <Bluetooth size={34} />}</span>
      </div>
      <h2 className={styles.heroTitle}>{gathering ? t.gather.pressTitle : t.gather.idleTitle}</h2>
      <p className={styles.heroBody}>
        {gathering ? (wide ? t.gather.pressBodyWide : t.gather.pressBody) : t.gather.idleBody}
      </p>
      <ConnectingLine />
    </section>
  )
}
