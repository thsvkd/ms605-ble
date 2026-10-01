import type { SiteGroup } from '../store/store'
import { SensorCard } from './SensorCard'
import styles from './dashboard.module.css'

export function SiteSection({ group }: { group: SiteGroup }) {
  const headId = `site-${group.site.site_id}`
  return (
    <section className={styles.section} aria-labelledby={headId}>
      <h2 id={headId} className={styles.sectionHead}>
        {group.site.name}
        <span className={styles.sectionCount}>({group.sensors.length})</span>
      </h2>
      <ul className={styles.grid}>
        {group.sensors.map((s) => (
          <li key={s.device_id}>
            <SensorCard deviceId={s.device_id} />
          </li>
        ))}
      </ul>
    </section>
  )
}
