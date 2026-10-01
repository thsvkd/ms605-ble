import type { ButtonHTMLAttributes } from 'react'
import type { LucideIcon } from 'lucide-react'
import styles from './ui.module.css'

export type ButtonVariant = 'primary' | 'secondary' | 'danger' | 'dangerSolid' | 'ghost'

interface Props extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant
  size?: 'md' | 'lg'
  block?: boolean
  icon?: LucideIcon
  iconSpin?: boolean
}

export function buttonClass(variant: ButtonVariant = 'secondary', size: 'md' | 'lg' = 'md', block = false): string {
  return [styles.button, styles[variant], size === 'lg' && styles.lg, block && styles.block].filter(Boolean).join(' ')
}

export function Button({ variant, size, block, icon: Icon, iconSpin, className, children, type, ...rest }: Props) {
  return (
    <button
      type={type ?? 'button'}
      className={[buttonClass(variant, size, block), className].filter(Boolean).join(' ')}
      {...rest}
    >
      {Icon && <Icon size={size === 'lg' ? 24 : 18} aria-hidden className={iconSpin ? 'spin' : undefined} />}
      {children}
    </button>
  )
}
