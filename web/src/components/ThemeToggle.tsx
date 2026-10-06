import { Moon, Sun, SunMoon } from 'lucide-react'
import { useState } from 'react'
import { useStrings } from '../strings'
import { applyTheme, nextTheme, readTheme, type ThemeChoice } from '../theme'
import styles from './shell.module.css'

const ICON = { system: SunMoon, light: Sun, dark: Moon } as const

/** Cycles system -> light -> dark (9.6). */
export function ThemeToggle() {
  const t = useStrings()
  const [choice, setChoice] = useState<ThemeChoice>(readTheme)
  const Icon = ICON[choice]
  return (
    <button
      type="button"
      className={styles.iconButton}
      aria-label={t.theme[choice]}
      title={t.theme[choice]}
      onClick={() => {
        const next = nextTheme(choice)
        applyTheme(next)
        setChoice(next)
      }}
    >
      <Icon size={20} aria-hidden />
    </button>
  )
}
