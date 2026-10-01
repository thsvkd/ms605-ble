import { KeyRound } from 'lucide-react'
import { t } from '../strings'
import styles from './shell.module.css'

export function AuthRequired() {
  return (
    <main className={styles.auth}>
      <div className={styles.authCard}>
        <div className={styles.authIcon} aria-hidden>
          <KeyRound size={32} />
        </div>
        <h1 className={styles.authTitle}>{t.auth.title}</h1>
        <p className={styles.authBody}>{t.auth.body}</p>
      </div>
    </main>
  )
}
