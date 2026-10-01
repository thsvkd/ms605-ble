import { useMemo } from 'react'
import { GatheredList } from '../components/GatheredList'
import { GatherHero } from '../components/GatherHero'
import { GatherToggle } from '../components/GatherToggle'
import { ImportSensorInfo } from '../components/ImportSensorInfo'
import { SimPanel } from '../components/SimPanel'
import styles from '../components/gather.module.css'
import { DESKTOP, useMediaQuery } from '../hooks/useMediaQuery'
import { selectGathered, useStore } from '../store/store'
import { t } from '../strings'

export function GatherScreen() {
  const sensors = useStore((s) => s.sensors)
  const hasGathered = useMemo(() => selectGathered({ sensors }).length > 0, [sensors])
  const desktop = useMediaQuery(DESKTOP)

  const title = <h1 className="visually-hidden">{t.nav.gather}</h1>

  if (desktop) {
    return (
      <div className={styles.screen}>
        {title}
        <div className={styles.side}>
          <GatherHero compact={false} wide />
          <GatherToggle />
          <ImportSensorInfo />
          <SimPanel />
        </div>
        <GatheredList />
      </div>
    )
  }

  // single column: hero, then the list and form right under it (thumb reach), toggle docked at the bottom
  return (
    <div className={styles.screen}>
      {title}
      <GatherHero compact={hasGathered} wide={false} />
      <GatheredList />
      <ImportSensorInfo />
      <SimPanel />
      <GatherToggle />
    </div>
  )
}
