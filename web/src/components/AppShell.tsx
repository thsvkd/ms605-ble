import { Bluetooth, LayoutGrid, Radar } from 'lucide-react'
import type { ReactNode } from 'react'
import { Link, useRoute } from 'wouter'
import { useStore } from '../store/store'
import { t } from '../strings'
import { ConnectionBanner } from './ConnectionBanner'
import { GatherPill } from './GatherPill'
import { LanBadge } from './LanBadge'
import { LiveRegion } from './LiveRegion'
import { Notices } from './Notices'
import { ThemeToggle } from './ThemeToggle'
import styles from './shell.module.css'

function NavLink({ href, className, children }: { href: string; className?: string; children: ReactNode }) {
  const [active] = useRoute(href)
  return (
    <Link href={href} className={className} aria-current={active ? 'page' : undefined}>
      {children}
    </Link>
  )
}

export function AppShell({ children }: { children: ReactNode }) {
  const open = useStore((s) => s.conn === 'open')
  const gathering = useStore((s) => s.gather.gathering)
  return (
    <div className={styles.app}>
      <header className={styles.header}>
        <div className={styles.headerInner}>
          <Link href="/" className={styles.brand}>
            <span className={styles.brandMark} aria-hidden>
              <Radar size={18} />
            </span>
            MS605
          </Link>
          <nav className={styles.tabs} aria-label={t.nav.label}>
            <NavLink href="/" className={styles.tab}>
              <LayoutGrid size={18} aria-hidden />
              {t.nav.dashboard}
            </NavLink>
            <NavLink href="/gather" className={styles.tab}>
              <Bluetooth size={18} aria-hidden />
              {t.nav.gather}
            </NavLink>
          </nav>
          <div className={styles.headerEnd}>
            <GatherPill />
            <LanBadge />
            <ThemeToggle />
          </div>
        </div>
      </header>
      <ConnectionBanner />
      <main className={`${styles.main} ${open ? '' : styles.stale}`}>{children}</main>
      <nav className={styles.bottomNav} aria-label={t.nav.label}>
        <NavLink href="/" className={styles.bottomLink}>
          <LayoutGrid size={22} aria-hidden />
          {t.nav.dashboard}
        </NavLink>
        <NavLink href="/gather" className={styles.bottomLink}>
          <Bluetooth size={22} aria-hidden />
          {gathering && <span className={styles.navDot} aria-hidden />}
          {t.nav.gather}
        </NavLink>
      </nav>
      <Notices />
      <LiveRegion />
    </div>
  )
}
