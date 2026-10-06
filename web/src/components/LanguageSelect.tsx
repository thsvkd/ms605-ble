import { Languages } from 'lucide-react'
import { setLocale, useLocale, useStrings } from '../strings'
import styles from './shell.module.css'

export function LanguageSelect({ className = '' }: { className?: string }) {
  const locale = useLocale()
  const t = useStrings()

  return (
    <label className={`${styles.languageSelect} ${className}`}>
      <Languages size={16} aria-hidden />
      <span className="visually-hidden">{t.language.label}</span>
      <select
        value={locale}
        aria-label={t.language.label}
        onChange={(event) => setLocale(event.target.value === 'en' ? 'en' : 'ko')}
      >
        <option value="ko">{t.language.ko}</option>
        <option value="en">{t.language.en}</option>
      </select>
    </label>
  )
}
