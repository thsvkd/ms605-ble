import { Activity, Bluetooth, Crosshair, LayoutGrid, Radar } from 'lucide-react'
import type { ReactNode } from 'react'
import { Link, useRoute } from 'wouter'
import { useBeforeUnloadGuard } from '../navGuard'
import { selectBatchActive, useStore } from '../store/store'
import { useStrings } from '../strings'
import { ApplyPill } from './ApplyPill'
import { CalibrationPill } from './CalibrationPill'
import { ConnectionBanner } from './ConnectionBanner'
import { GatherPill } from './GatherPill'
import { LanBadge } from './LanBadge'
import { LanguageSelect } from './LanguageSelect'
import { LiveRegion } from './LiveRegion'
import { Notices } from './Notices'
import { ThemeToggle } from './ThemeToggle'
import { UnsavedChangesDialog } from './UnsavedChangesDialog'
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
  const t = useStrings()
  const open = useStore((s) => s.conn === 'open')
  const gathering = useStore((s) => s.gather.gathering)
  const batchActive = useStore(selectBatchActive)
  useBeforeUnloadGuard()
  return (
    <div className={styles.app}>
      <header className={styles.header}>
        <div className={styles.headerInner}>
          <Link href="/" className={styles.brand} aria-label="MS605">
            <span className={styles.brandMark} aria-hidden>
              <Radar size={18} />
            </span>
            <span className={styles.squeeze}>MS605</span>
          </Link>
          <nav className={styles.tabs} aria-label={t.nav.label}>
            <NavLink href="/" className={styles.tab}>
              <LayoutGrid size={18} aria-hidden />
              <span className={styles.tabLabel}>{t.nav.dashboard}</span>
            </NavLink>
            <NavLink href="/gather" className={styles.tab}>
              <Bluetooth size={18} aria-hidden />
              <span className={styles.tabLabel}>{t.nav.gather}</span>
            </NavLink>
            <NavLink href="/monitor" className={styles.tab}>
              <Activity size={18} aria-hidden />
              <span className={styles.tabLabel}>{t.nav.monitor}</span>
            </NavLink>
            <NavLink href="/calibrate" className={styles.tab}>
              <Crosshair size={18} aria-hidden />
              <span className={styles.tabLabel}>{t.nav.calibrate}</span>
            </NavLink>
          </nav>
          <div className={styles.headerEnd}>
            <ApplyPill />
            <CalibrationPill />
            <GatherPill />
            <LanBadge />
            <LanguageSelect />
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
        <NavLink href="/monitor" className={styles.bottomLink}>
          <Activity size={22} aria-hidden />
          {t.nav.monitor}
        </NavLink>
        <NavLink href="/calibrate" className={styles.bottomLink}>
          <Crosshair size={22} aria-hidden />
          {batchActive && <span className={styles.navDot} aria-hidden />}
          {t.nav.calibrate}
        </NavLink>
      </nav>
      <Notices />
      <LiveRegion />
      <UnsavedChangesDialog />
    </div>
  )
}
