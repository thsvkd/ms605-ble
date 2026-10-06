import { describe, expect, it } from 'vitest'
import { sensorStatus } from '../status'
import { live, registry, SITE_A, sensor } from './fixtures'

const NOW = Date.parse('2026-10-01T12:00:00Z')

describe('sensorStatus (9.3)', () => {
  it('connected + busy -> progress with the busy wording', () => {
    const s = sensorStatus(sensor(1, { live: live(1, { busy: 'calibration' }) }), false, NOW)
    expect(s).toMatchObject({ kind: 'progress', label: '작업 중 (보정 중)' })
  })

  it('unknown busy reasons are shown verbatim', () => {
    expect(sensorStatus(sensor(1, { live: live(1, { busy: 'mystery' }) }), false, NOW).label).toBe('작업 중 (mystery)')
  })

  it('connected -> ok', () => {
    expect(sensorStatus(sensor(1, { live: live(1) }), false, NOW)).toMatchObject({ kind: 'ok', label: '연결됨' })
  })

  it('connecting -> progress with a spinning icon', () => {
    const s = sensorStatus(sensor(1, { live: live(1, { link: 'connecting' }) }), false, NOW)
    expect(s).toMatchObject({ kind: 'progress', label: '연결 중…', spin: true })
  })

  it('lost -> warn, and the hint depends on whether gathering is on', () => {
    const v = sensor(1, { live: live(1, { link: 'lost' }) })
    expect(sensorStatus(v, true, NOW)).toMatchObject({ kind: 'warn', label: '연결 끊김', hint: '센서 버튼을 다시 누르세요' })
    expect(sensorStatus(v, false, NOW).hint).toBe('센서 추가를 켜고 버튼을 누르세요')
  })

  it('disconnected -> off', () => {
    const s = sensorStatus(sensor(1, { live: live(1, { link: 'disconnected' }) }), false, NOW)
    expect(s).toMatchObject({ kind: 'off', label: '연결 해제됨' })
  })

  it.each(['lost', 'disconnected'] as const)('remembered %s sensor explains automatic recovery', (link) => {
    const s = sensorStatus(sensor(1, { live: live(1, { link, auto_reconnect: true }) }), false, NOW)
    expect(s.hint).toContain('1초 간격')
    expect(s.hint).not.toContain('센서 추가를 켜고')
    if (link === 'disconnected') expect(s.label).toBe('재연결 대기')
    expect(sensorStatus(sensor(1, { live: live(1, { link, auto_reconnect: true }) }), true, NOW).hint)
      .toBe('센서 버튼을 다시 누르세요')
  })

  it('no session -> off with last-seen hint', () => {
    const seen = sensor(1, { registry: registry(SITE_A, 'a', { last_seen: '2026-10-01T10:00:00Z' }) })
    expect(sensorStatus(seen, false, NOW)).toMatchObject({ kind: 'off', label: '연결 안 됨', hint: '마지막 확인 2시간 전' })
    const never = sensor(2, { registry: registry(SITE_A, 'b') })
    expect(sensorStatus(never, false, NOW).hint).toBe('확인 기록 없음')
  })

  it('label is never empty', () => {
    const links = ['connected', 'connecting', 'lost', 'disconnected'] as const
    const views = [sensor(1), ...links.map((link) => sensor(1, { live: live(1, { link }) }))]
    for (const v of views) {
      for (const g of [true, false]) expect(sensorStatus(v, g, NOW).label.length).toBeGreaterThan(0)
    }
  })
})
