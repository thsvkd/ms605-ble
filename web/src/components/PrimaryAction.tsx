import { ArrowRight, Plus } from 'lucide-react'
import { Link } from 'wouter'
import { useStore } from '../store/store'
import { useStrings } from '../strings'
import { buttonClass } from './Button'

/** The dashboard's one primary action (10.1-1). */
export function PrimaryAction() {
  const t = useStrings()
  const gathering = useStore((s) => s.gather.gathering)
  const Icon = gathering ? ArrowRight : Plus
  return (
    <Link href="/gather" className={buttonClass('primary', 'md', true)}>
      <Icon size={18} aria-hidden />
      {gathering ? t.dash.gatheringCta : t.nav.gather}
    </Link>
  )
}
