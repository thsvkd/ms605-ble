import { SlidersHorizontal } from 'lucide-react'
import { useMemo } from 'react'
import { Link } from 'wouter'
import { buttonClass } from '../components/Button'
import { EmptyState } from '../components/EmptyState'
import { PendingSection } from '../components/PendingSection'
import { PresenceLegend } from '../components/PresenceSignals'
import { PrimaryAction } from '../components/PrimaryAction'
import { ReleaseAllButton } from '../components/ReleaseAllButton'
import { SiteSection } from '../components/SiteSection'
import { SummaryBar } from '../components/SummaryBar'
import { UnregisteredSection } from '../components/UnregisteredSection'
import styles from '../components/dashboard.module.css'
import { useLiveWatch } from '../hooks/useLiveWatch'
import { DESKTOP, useMediaQuery } from '../hooks/useMediaQuery'
import { selectBySite, useStore } from '../store/store'
import { useStrings } from '../strings'

export function DashboardScreen() {
  const t = useStrings()
  const sensors = useStore((s) => s.sensors)
  const sites = useStore((s) => s.sites)
  const pending = useStore((s) => s.pending)
  const groups = useMemo(() => selectBySite({ sensors, sites }), [sensors, sites])
  const desktop = useMediaQuery(DESKTOP)
  const empty = Object.keys(sensors).length === 0
  const sessions = Object.values(sensors).some((s) => s.live !== null)
  // Live presence on the cards: registered, connected sensors only (useLiveWatch adds "while the tab is
  // visible"). A sensor that disconnects leaves the list and is unwatched alone; the others keep theirs.
  const connected = useMemo(
    () => groups.flatMap((g) => g.sensors.filter((x) => x.live?.link === 'connected').map((x) => x.device_id)),
    [groups],
  )
  useLiveWatch(connected)

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
          {connected.length > 0 && <PresenceLegend />}
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
