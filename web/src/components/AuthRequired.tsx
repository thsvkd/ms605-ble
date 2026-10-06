import { KeyRound } from 'lucide-react'
import { useStrings } from '../strings'
import { LanguageSelect } from './LanguageSelect'
import styles from './shell.module.css'

export function AuthRequired() {
  const t = useStrings()
  return (
    <main className={styles.auth}>
      <LanguageSelect className={styles.authLanguage} />
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
