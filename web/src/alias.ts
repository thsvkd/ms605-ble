import type { SensorView } from './api/types'
import { t } from './strings'

/** `센서 {n}`: n starts at the site's registered count + 1 and rises until unused in that site (9.4). */
export function defaultAlias(sensors: Record<string, SensorView>, siteId: string | null): string {
  const taken = new Set<string>()
  if (siteId !== null) {
    for (const s of Object.values(sensors)) {
      if (s.registry?.site_id === siteId) taken.add(s.registry.alias)
    }
  }
  let n = taken.size + 1
  while (taken.has(t.sensor.defaultAlias(n))) n += 1
  return t.sensor.defaultAlias(n)
}

const LAST_SITE_KEY = 'ms605.lastSiteId'

export function readLastSite(): string | null {
  try {
    return localStorage.getItem(LAST_SITE_KEY)
  } catch {
    return null
  }
}

export function writeLastSite(siteId: string): void {
  try {
    localStorage.setItem(LAST_SITE_KEY, siteId)
  } catch {
    // convenience only
  }
}
