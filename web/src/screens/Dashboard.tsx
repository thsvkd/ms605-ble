import { SlidersHorizontal } from 'lucide-react'
import { useMemo } from 'react'
import { Link } from 'wouter'
import { buttonClass } from '../components/Button'
import { EmptyState } from '../components/EmptyState'
import { PendingSection } from '../components/PendingSection'
import { PrimaryAction } from '../components/PrimaryAction'
import { ReleaseAllButton } from '../components/ReleaseAllButton'
import { SiteSection } from '../components/SiteSection'
import { SummaryBar } from '../components/SummaryBar'
import { UnregisteredSection } from '../components/UnregisteredSection'
import styles from '../components/dashboard.module.css'
import { DESKTOP, useMediaQuery } from '../hooks/useMediaQuery'
import { selectBySite, useStore } from '../store/store'
import { t } from '../strings'

export function DashboardScreen() {
  const sensors = useStore((s) => s.sensors)
  const sites = useStore((s) => s.sites)
  const pending = useStore((s) => s.pending)
  const groups = useMemo(() => selectBySite({ sensors, sites }), [sensors, sites])
  const desktop = useMediaQuery(DESKTOP)
  const empty = Object.keys(sensors).length === 0
  const sessions = Object.values(sensors).some((s) => s.live !== null)

  return (
    <div className={styles.screen}>
      <h1 className="visually-hidden">{t.nav.dashboard}</h1>
      {empty && pending.length === 0 ? (
        <EmptyState />
      ) : (
        <>
          <div className={styles.top}>
            <SummaryBar />
            <div className={styles.topActions}>
              {desktop && <ReleaseAllButton />}
              {sessions && (
                <Link href="/bulk" className={buttonClass('secondary', 'md', !desktop)}>
                  <SlidersHorizontal size={18} aria-hidden />
                  {t.dash.bulk}
                </Link>
              )}
              <PrimaryAction />
            </div>
          </div>
          <UnregisteredSection />
          {groups.map((g) => (
            <SiteSection key={g.site.site_id} group={g} />
          ))}
          <PendingSection />
          {!desktop && <ReleaseAllButton className={styles.releaseMobile} />}
        </>
      )}
    </div>
  )
}
