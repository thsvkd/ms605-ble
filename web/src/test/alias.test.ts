import { describe, expect, it } from 'vitest'
import { defaultAlias, sensorDisplayName } from '../alias'
import type { SensorView } from '../api/types'
import { live, registry, SITE_A, SITE_B, sensor } from './fixtures'

const map = (...views: SensorView[]) => Object.fromEntries(views.map((v) => [v.device_id, v]))

describe('defaultAlias', () => {
  it('prefers the device MAC over the host BLE address and advertised name', () => {
    expect(defaultAlias(
      {},
      'lab-a',
      live(1, { mac: '84:CC:A8:12:34:56', address: '02:00:00:ab:cd:ef', name: 'RFBL_ABCDEF' }),
    )).toBe('MS605-123456')
  })

  it.each(['02:00:00:ab:cd:ef', '02-00-00-AB-CD-EF', '020000aBcDeF'])(
    'uses the last six MAC characters as contiguous uppercase hex: %s',
    (address) => {
      expect(defaultAlias({}, 'lab-a', live(1, { address }))).toBe('MS605-ABCDEF')
    },
  )

  it('preserves leading zeroes and prefers the MAC over the advertised name', () => {
    expect(defaultAlias({}, null, live(1, { name: 'RFBL_ABCDEF' }))).toBe('MS605-000001')
  })

  it.each(['RFBL_ab12cd', 'MRBL_AB12CD'])(
    'uses the advertised suffix when CoreBluetooth provides a UUID: %s',
    (name) => {
      expect(defaultAlias({}, null, live(1, { address: '00000000-0000-4000-8000-000000000001', name })))
        .toBe('MS605-AB12CD')
    },
  )

  it.each([
    live(1, { address: '00000000-0000-4000-8000-000000000001', name: null }),
    live(1, { address: 'invalid', name: 'MRBL_SIM01' }),
    live(1, { address: '', name: 'other_ABCDEF' }),
    live(1, { address: '02:00-00:AB:CD:EF', name: null }),
    live(1, { address: '0200:00ABCDEF', name: null }),
    null,
  ])('keeps the sequential fallback when no MAC suffix is available', (identity) => {
    expect(defaultAlias({}, 'lab-a', identity)).toBe('센서 1')
  })

  it('keeps the MAC suffix while avoiding names taken in the selected site', () => {
    const m = map(
      sensor(1, { registry: registry(SITE_A, 'MS605-ABCDEF') }),
      sensor(2, { registry: registry(SITE_A, 'MS605-ABCDEF 2') }),
      sensor(3, { registry: registry(SITE_B, 'MS605-ABCDEF 3') }),
    )
    const identity = live(4, { address: '02:00:00:ab:cd:ef' })
    expect(defaultAlias(m, 'lab-a', identity)).toBe('MS605-ABCDEF 3')
    expect(defaultAlias(m, 'lab-b', identity)).toBe('MS605-ABCDEF')
    expect(defaultAlias(m, null, identity)).toBe('MS605-ABCDEF')
  })

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

describe('sensorDisplayName', () => {
  it('shows the actual advertised MAC suffix before the BLE name or host address', () => {
    const view = sensor(1, { live: live(1, { mac: '84:CC:A8:12:34:56', name: 'ms605' }) })
    expect(sensorDisplayName(view)).toBe('MS605-123456')
  })

  it('keeps a registered alias ahead of the advertised MAC', () => {
    const view = sensor(1, {
      registry: registry(SITE_A, '북쪽 벽'),
      live: live(1, { mac: '84:CC:A8:12:34:56' }),
    })
    expect(sensorDisplayName(view)).toBe('북쪽 벽')
  })
})
