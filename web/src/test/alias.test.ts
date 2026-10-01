import { describe, expect, it } from 'vitest'
import { defaultAlias } from '../alias'
import type { SensorView } from '../api/types'
import { registry, SITE_A, SITE_B, sensor } from './fixtures'

const map = (...views: SensorView[]) => Object.fromEntries(views.map((v) => [v.device_id, v]))

describe('defaultAlias', () => {
  it('starts at 1 for an empty or new site', () => {
    expect(defaultAlias({}, 'lab-a')).toBe('센서 1')
    expect(defaultAlias(map(sensor(1, { registry: registry(SITE_A, '센서 1') })), null)).toBe('센서 1')
  })

  it('starts at the site count + 1', () => {
    const m = map(sensor(1, { registry: registry(SITE_A, '북쪽') }), sensor(2, { registry: registry(SITE_A, '남쪽') }))
    expect(defaultAlias(m, 'lab-a')).toBe('센서 3')
  })

  it('skips aliases already used in that site only', () => {
    const m = map(
      sensor(1, { registry: registry(SITE_A, '센서 2') }),
      sensor(2, { registry: registry(SITE_A, '센서 3') }),
      sensor(3, { registry: registry(SITE_B, '센서 4') }),
    )
    expect(defaultAlias(m, 'lab-a')).toBe('센서 4')
    expect(defaultAlias(m, 'lab-b')).toBe('센서 2')
  })
})
