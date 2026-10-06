import type { LiveInfo, SensorView } from './api/types'
import { t } from './strings'

function macSuffix(value: string | null | undefined): string | null {
  return /^(?:[0-9a-f]{12}|(?:[0-9a-f]{2}:){5}[0-9a-f]{2}|(?:[0-9a-f]{2}-){5}[0-9a-f]{2})$/i.test(value ?? '')
    ? value!.replace(/[:-]/g, '').slice(-6).toUpperCase()
    : null
}

/** Registered alias, else actual advertised MAC, then the existing BLE identity fallbacks. */
export function sensorDisplayName(sensor: SensorView | undefined, fallback = ''): string {
  const suffix = macSuffix(sensor?.live?.mac)
  return sensor?.registry?.alias ?? (suffix ? t.sensor.macAlias(suffix) : null) ??
    sensor?.live?.name ?? sensor?.live?.address ?? (sensor?.device_id || fallback)
}

/** Prefer a MAC suffix; fall back to an unused site sequence when unavailable. */
export function defaultAlias(
  sensors: Record<string, SensorView>,
  siteId: string | null,
  identity?: Pick<LiveInfo, 'mac' | 'address' | 'name'> | null,
): string {
  const taken = new Set<string>()
  if (siteId !== null) {
    for (const s of Object.values(sensors)) {
      if (s.registry?.site_id === siteId) taken.add(s.registry.alias)
    }
  }
  const suffix = macSuffix(identity?.mac) ?? macSuffix(identity?.address) ??
    identity?.name?.match(/^(?:RFBL|MRBL)_([0-9a-f]{6})$/i)?.[1]?.toUpperCase()
  if (suffix) {
    const base = t.sensor.macAlias(suffix.toUpperCase())
    let alias = base
    let n = 2
    while (taken.has(alias)) alias = `${base} ${n++}`
    return alias
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
