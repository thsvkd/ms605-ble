import { Link } from 'wouter'
import type { Edit } from '../../draft'
import { useDrafts } from '../../store/drafts'
import { t } from '../../strings'
import styles from './edit.module.css'

export type SensorTab = 'info' | 'settings' | 'advanced' | 'history'

// which part of the one shared draft each tab edits, so a dot marks only the tab whose fields changed
const settingsDirty = (e: Edit) =>
  e.sensitivity !== null || e.zone_enable !== null || e.trigger.some((v) => v !== null) || e.maintain.some((v) => v !== null)
const advancedDirty = (e: Edit) =>
  e.subsensor_zones !== null || e.subsensor_timing !== null || e.subsensor_enable !== null || e.dnd !== null

const TABS: { tab: SensorTab; path: string; label: string; dirty: ((e: Edit) => boolean) | null }[] = [
  { tab: 'info', path: '', label: t.detail.tabInfo, dirty: null },
  { tab: 'settings', path: '/settings', label: t.detail.tabSettings, dirty: settingsDirty },
  { tab: 'advanced', path: '/advanced', label: t.detail.tabAdvanced, dirty: advancedDirty },
  { tab: 'history', path: '/history', label: t.detail.tabHistory, dirty: null },
]

/** Four links (tabs are routes, not an ARIA tablist); a dot on 설정/고급 while that tab's fields have changes. */
export function SensorTabs({ deviceId, current }: { deviceId: string; current: SensorTab }) {
  const edit = useDrafts((s) => s.sensors[deviceId]?.edit)
  const base = `/sensors/${encodeURIComponent(deviceId)}`
  return (
    <nav aria-label={t.detail.tabs} className={styles.tabs}>
      {TABS.map(({ tab, path, label, dirty }) => (
        <Link
          key={tab}
          href={`${base}${path}`}
          className={styles.tab}
          aria-current={tab === current ? 'page' : undefined}
        >
          {label}
          {edit !== undefined && dirty?.(edit) && (
            <>
              <span className={styles.tabDot} aria-hidden />
              <span className="visually-hidden">{` (${t.edit.tabDirty})`}</span>
            </>
          )}
        </Link>
      ))}
    </nav>
  )
}
