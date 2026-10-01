import { act, fireEvent, render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it } from 'vitest'
import { LiveStrip } from '../components/LiveStrip'
import { Meter } from '../components/Meter'
import { VerticalMeter } from '../components/MonitorWall'
import { meter } from '../meter'
import { idsParam, MonitorScreen } from '../screens/Monitor'
import { resetStore, useStore } from '../store/store'
import { deviceId, live, liveData, NOW_S, registry, SITE_A, sensor, storeState } from './fixtures'
import cases from './meter_cases.json'

interface MeterCase {
  value: number
  threshold: number
  width: number
  tick_at: number
  plain: string
  over: boolean
}

const at = (path: string) => window.history.replaceState(null, '', path)
const pushLive = (n: number, patch: Parameters<typeof liveData>[1] = {}) =>
  act(() => {
    useStore.getState().applyMessage({ type: 'live', seq: null, ts: NOW_S, data: liveData(n, patch) })
  })
const setVisibility = (state: DocumentVisibilityState) => {
  Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => state })
  act(() => {
    document.dispatchEvent(new Event('visibilitychange'))
  })
}

afterEach(() => {
  at('/')
  Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'visible' })
})

describe('Meter', () => {
  it('draws the cells of meter() and says over/under in words', () => {
    const { container, rerender } = render(<Meter value={64} threshold={60} label="재실 트리거" hot />)
    const cells = [...container.querySelectorAll('[data-cell]')].map((c) => c.getAttribute('data-cell'))
    expect(cells).toEqual(meter(64, 60).cells)
    expect(screen.getByRole('img')).toHaveAccessibleName('재실 트리거 64, 임계값 60, 초과')
    expect(container.querySelector('[data-over]')).toHaveAttribute('data-over', 'true')
    expect(screen.getByText('64/60')).toHaveAttribute('data-hot', 'true')

    rerender(<Meter value={60} threshold={60} label="재실 유지" hot={false} />)
    expect(screen.getByRole('img')).toHaveAccessibleName('재실 유지 60, 임계값 60, 이하')
    expect(screen.getByText('60/60')).toHaveAttribute('data-hot', 'false')
  })

  it('handles a negative calibration threshold like the CLI', () => {
    const { container } = render(<Meter value={-10} threshold={-33} label="재실 트리거" hot={false} />)
    expect(container.querySelector('[data-over]')).toHaveAttribute('data-over', 'true')
    expect(screen.getByRole('img')).toHaveAccessibleName(/초과$/)
  })
})

describe('VerticalMeter', () => {
  const plain = (cells: (string | null)[]) => cells.map((c) => (c === 'fill' ? '█' : c === 'tick' ? '┃' : '─')).join('')

  it.each(cases as MeterCase[])('stands meter() on end: $value/$threshold (w=$width, tick=$tick_at)', (c) => {
    const model = meter(c.value, c.threshold, c.width, c.tick_at)
    const { container } = render(<VerticalMeter model={model} />)
    const track = container.querySelector('[data-meter="vertical"]') as HTMLElement
    // DOM order is cell 0 first; the track stacks it bottom-up (column-reverse), so the tick row never moves
    const cells = [...track.querySelectorAll('[data-cell]')].map((el) => el.getAttribute('data-cell'))
    expect(cells).toEqual(model.cells)
    expect(plain(cells)).toBe(c.plain)
    expect(track).toHaveAttribute('data-over', String(c.over))
  })
})

describe('MonitorScreen', () => {
  const sensors = () => [
    sensor(1, { registry: registry(SITE_A, '센서 1', { location: '북쪽 벽' }), live: live(1) }),
    sensor(2, { registry: registry(SITE_A, '센서 2'), live: live(2, { link: 'lost', lost_reason: 'idle' }) }),
    sensor(3, { registry: registry(SITE_A, '센서 3'), live: live(3) }),
  ]
  const QUIET = {
    pir: false,
    sub_sensor_presence: [false, false, false],
    zones: liveData(1).zones.map((z) => ({ ...z, trigger: 10, trigger_active: false })),
  }

  const tileOf = (name: string) => screen.getByRole('listitem', { name })
  const wall = () => screen.getByRole('list', { name: '센서별 실시간 상태' })
  const zone = (tile: HTMLElement, i: number) => within(tile).getByRole('button', { name: new RegExp(`^Z${i} · `) })
  const readout = (tile: HTMLElement) => tile.querySelector('[class*="readout"]')

  it('starts with every connected sensor: one tile each, a summary and a compact legend', () => {
    resetStore(storeState({ sensors: sensors() }))
    at('/monitor')
    render(<MonitorScreen />)
    expect(useStore.getState().watch).toEqual({ [deviceId(1)]: 1, [deviceId(3)]: 1 })
    expect(screen.getByRole('button', { name: '센서 1' })).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByRole('button', { name: '센서 2' })).toHaveAttribute('aria-pressed', 'false')
    expect(within(wall()).getAllByRole('listitem')).toHaveLength(2)
    expect(screen.queryByRole('table')).not.toBeInTheDocument()

    const tile = tileOf('센서 1')
    expect(within(tile).getByRole('link', { name: '센서 1' })).toHaveAttribute('href', `/sensors/${deviceId(1)}`)
    expect(tile).toHaveTextContent('북쪽 벽')
    expect(tile).toHaveTextContent('데이터 수신 대기 중…')

    // compact keys always; the full definitions behind a disclosure
    expect(screen.getByText('막대 = 존 트리거 · 가로선 = 임계값')).toBeInTheDocument()
    const more = screen.getByRole('button', { name: '범례 자세히' })
    expect(more).toHaveAttribute('aria-expanded', 'false')
    expect(screen.queryByText('기기 판정 (S1~S3 중 하나라도) · PIR 포함 여부 미확인')).not.toBeInTheDocument()
    fireEvent.click(more)
    expect(more).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getByText('기기 판정 (S1~S3 중 하나라도) · PIR 포함 여부 미확인')).toBeInTheDocument()
    expect(screen.getByText(/가로선 = 임계값\(고정 위치\)/)).toBeInTheDocument()
  })

  it('a tile leads with the device call: ? before a frame, then 재실 (blue) or 부재, with PIR / RF blocks', () => {
    resetStore(storeState({ sensors: sensors() }))
    at('/monitor')
    render(<MonitorScreen />)
    const tile = tileOf('센서 1')
    expect(tile).toHaveAttribute('data-tone', 'unknown')
    expect(within(tile).getByRole('img', { name: '재실(기기 판정): 알 수 없음, 아직 받은 값 없음' })).toHaveTextContent('?')
    expect(within(tile).getByRole('img', { name: 'PIR: 알 수 없음, 아직 받은 값 없음' })).toHaveAttribute('data-tone', 'unknown')
    expect(within(tile).getByRole('img', { name: 'S1~S3 알 수 없음' })).toBeInTheDocument()
    expect(within(tile).queryAllByRole('button')).toHaveLength(0) // no zone values to open yet
    expect(tile).toHaveTextContent('존 값 없음')

    pushLive(1)
    expect(tile).toHaveAttribute('data-present', 'true')
    expect(tile).toHaveAttribute('data-tone', 'present')
    expect(within(tile).getByRole('img', { name: '재실(기기 판정): 재실' })).toHaveTextContent('재실')
    expect(within(tile).getByText('기기 판정')).toHaveAttribute('title', '기기 판정 · PIR 포함 여부 미확인')
    expect(within(tile).getByText('기기 판정')).toHaveTextContent('기기 판정 · PIR 포함 여부 미확인') // spoken too
    expect(within(tile).getByRole('img', { name: 'S1 재실, S2 부재, S3 부재' })).toBeInTheDocument()
    const pir = within(tile).getByRole('img', { name: 'PIR: 감지' })
    expect(pir).toHaveAttribute('data-tone', 'hit')
    expect(pir).toHaveTextContent('PIR 감지')
    expect(within(tile).getByRole('img', { name: 'RF(레이더): 감지' })).toHaveTextContent('RF 감지')

    pushLive(1, QUIET)
    expect(tile).toHaveAttribute('data-present', 'false')
    expect(within(tile).getByRole('img', { name: '재실(기기 판정): 부재' })).toHaveTextContent('부재')
    expect(within(tile).getByRole('img', { name: 'PIR: 감지 없음' })).toHaveTextContent('PIR 없음')
    expect(within(tile).getByRole('img', { name: 'RF(레이더): 감지 없음' })).toHaveAttribute('data-tone', 'off')

    pushLive(1, { ...QUIET, pir: null })
    expect(within(tile).getByRole('img', { name: 'PIR: 알 수 없음, 아직 받은 값 없음' })).toHaveTextContent('PIR ?')
  })

  it('draws Z0..Z6 as vertical trigger meters from meter(), with disabled zones hatched', () => {
    resetStore(storeState({ sensors: sensors() }))
    at('/monitor')
    render(<MonitorScreen />)
    pushLive(1)
    const tile = tileOf('센서 1')
    const zones = within(within(tile).getByRole('group', { name: '존별 재실 트리거' })).getAllByRole('button')
    expect(zones.map((b) => b.textContent)).toEqual(['Z0', 'Z1', 'Z2', 'Z3', 'Z4', 'Z5', 'Z6'])
    const frame = liveData(1)
    for (const z of frame.zones.filter((z) => z.enabled)) {
      const cells = [...zones[z.index]!.querySelectorAll('[data-cell]')].map((c) => c.getAttribute('data-cell'))
      expect(cells).toEqual(meter(z.trigger, z.trigger_threshold).cells)
    }
    expect(zones[0]).toHaveAttribute('data-over', 'true') // 64 > 60
    expect(zones[1]).toHaveAttribute('data-over', 'false')
    expect(zones[2]).toHaveAttribute('data-over', 'true') // -10 > -33
    const off = zones[6]!
    expect(off).toHaveAttribute('data-off')
    expect(off.querySelector('[data-cell]')).toBeNull()
    expect(off.querySelector('[data-off]')).toBeInTheDocument()
    expect(off).toHaveAccessibleName('Z6 · 5.6 m · 꺼짐')
  })

  it('each zone names its numbers and shows them on hover, focus or a tap', async () => {
    const user = userEvent.setup()
    resetStore(storeState({ sensors: sensors() }))
    at('/monitor')
    render(<MonitorScreen />)
    pushLive(1)
    const tile = tileOf('센서 1')
    const z0 = zone(tile, 0)
    expect(z0).toHaveAccessibleName('Z0 · 0.8 m · 트리거 64/60 · 유지 22/30 · RF 감지 (기기 트리거), 트리거 임계값 초과')
    expect(zone(tile, 2)).toHaveAccessibleName('Z2 · 2.4 m · 트리거 -10/-33 · 유지 5/0, 트리거 임계값 초과')
    expect(zone(tile, 3)).toHaveAccessibleName('Z3 · 3.2 m · 트리거 40/55 · 유지 20/30, 트리거 임계값 이하')
    expect(readout(tile)).toBeNull()

    await user.hover(z0)
    expect(readout(tile)).toHaveTextContent('Z0 · 0.8 m · 트리거 64/60 · 유지 22/30')
    expect(readout(tile)).toHaveAttribute('aria-hidden', 'true') // the button's name already says it
    await user.unhover(z0)
    expect(readout(tile)).toBeNull()

    // keyboard: focus opens, Escape closes, the numbers follow the live frame
    act(() => zone(tile, 3).focus())
    expect(readout(tile)).toHaveTextContent('Z3 · 3.2 m · 트리거 40/55 · 유지 20/30')
    pushLive(1, { zones: liveData(1).zones.map((z) => (z.index === 3 ? { ...z, trigger: 70, trigger_active: true } : z)) })
    // the device's own trigger flag (what RF counts) is named and marked on its zone
    expect(readout(tile)).toHaveTextContent('Z3 · 3.2 m · 트리거 70/55 · 유지 20/30 · RF 감지 (기기 트리거)')
    expect(zone(tile, 3)).toHaveAttribute('data-rf', 'true')
    expect(zone(tile, 3)).toHaveAccessibleName(/RF 감지 \(기기 트리거\)/)
    expect(zone(tile, 4)).not.toHaveAttribute('data-rf')
    expect(zone(tile, 4)).not.toHaveAccessibleName(/RF 감지/)
    await user.keyboard('{Escape}')
    expect(readout(tile)).toBeNull()
    act(() => zone(tile, 3).blur())

    // Enter on a focused zone keeps (or brings back) its numbers, never closes them
    act(() => zone(tile, 4).focus())
    await user.keyboard('{Enter}')
    expect(readout(tile)).toHaveTextContent(/^Z4 · /)
    await user.keyboard('{Escape}')
    await user.keyboard('{Enter}')
    expect(readout(tile)).toHaveTextContent(/^Z4 · /)
    act(() => zone(tile, 4).blur())

    // touch: a tap opens, a second tap closes, a tap elsewhere closes
    const z2 = zone(tile, 2)
    await user.pointer({ keys: '[TouchA]', target: z2 })
    expect(readout(tile)).toHaveTextContent('Z2 · 2.4 m · 트리거 -10/-33 · 유지 5/0')
    await user.pointer({ keys: '[TouchA]', target: z2 })
    expect(readout(tile)).toBeNull()
    await user.pointer({ keys: '[TouchA]', target: z2 })
    expect(readout(tile)).not.toBeNull()
    await user.pointer({ keys: '[TouchA]', target: screen.getByRole('heading', { name: '실시간 모니터' }) })
    expect(readout(tile)).toBeNull()
  })

  it('takes each sensor\'s own zone distances', () => {
    resetStore(storeState({ sensors: sensors() }))
    at('/monitor')
    render(<MonitorScreen />)
    pushLive(1)
    pushLive(3, { zones: liveData(3).zones.map((z) => ({ ...z, distance_m: 0.6 * (z.index + 1) })) })
    expect(zone(tileOf('센서 1'), 3)).toHaveAccessibleName(/^Z3 · 3\.2 m · /)
    expect(zone(tileOf('센서 3'), 3)).toHaveAccessibleName(/^Z3 · 2\.4 m · /)
  })

  it('counts the summary over the connected sensors: 재실, PIR, RF and 값 없음', () => {
    resetStore(storeState({ sensors: sensors() }))
    at('/monitor')
    render(<MonitorScreen />)
    const stats = () =>
      within(screen.getByRole('list', { name: '지금 감지 요약' }))
        .getAllByRole('listitem')
        .map((li) => li.textContent)
    expect(stats()).toEqual(['0재실 / 2대', '0PIR 감지', '0RF 감지', '2값 없음'])
    pushLive(1)
    expect(stats()).toEqual(['1재실 / 2대', '1PIR 감지', '1RF 감지', '1값 없음']) // sensor 3 has no frame yet
    pushLive(3, { pir: false, sub_sensor_presence: [false, true, false] })
    expect(stats()).toEqual(['2재실 / 2대', '1PIR 감지', '2RF 감지'])
  })

  it('takes ?ids=, shows a lost sensor dimmed with its hint, and drops a tile when its chip is turned off', () => {
    resetStore(storeState({ sensors: sensors() }))
    at(`/monitor?ids=${deviceId(1)},${deviceId(2)}`)
    render(<MonitorScreen />)
    const lost = tileOf('센서 2')
    expect(lost).toHaveAttribute('data-stale', 'true')
    expect(within(lost).getByText('센서 모으기를 켜고 버튼을 누르세요')).toBeInTheDocument()
    pushLive(2)
    expect(within(lost).getByText('실시간 값 없음 (마지막 값)')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '센서 2' }))
    expect(useStore.getState().watch).toEqual({ [deviceId(1)]: 1 })
    expect(screen.queryByRole('listitem', { name: '센서 2' })).not.toBeInTheDocument()
    expect(within(wall()).getAllByRole('listitem')).toHaveLength(1)
    expect(JSON.parse(localStorage.getItem('ms605.monitorIds') ?? '[]')).toEqual([deviceId(1)])

    fireEvent.click(screen.getByRole('button', { name: '모두 해제' }))
    expect(useStore.getState().watch).toEqual({})
    expect(screen.getByText('볼 센서를 고르세요')).toBeInTheDocument()
    expect(screen.queryByRole('list', { name: '센서별 실시간 상태' })).not.toBeInTheDocument()
  })

  it("never counts or tints a lost sensor's last frame as a detection now", () => {
    resetStore(storeState({ sensors: sensors() }))
    at(`/monitor?ids=${deviceId(1)},${deviceId(2)}`)
    render(<MonitorScreen />)
    pushLive(1, { pir: false, sub_sensor_presence: [false, false, false], zones: [] })
    pushLive(2) // lost sensor 2: its last frame says 재실, PIR and RF
    const lost = tileOf('센서 2')
    expect(within(lost).getByText('실시간 값 없음 (마지막 값)')).toBeInTheDocument()
    expect(lost).toHaveAttribute('data-present', 'false') // no blue tint on history
    expect(lost).toHaveAttribute('data-tone', 'stale') // nor a blue 재실 word

    const summary = screen.getByRole('list', { name: '지금 감지 요약' })
    const stats = within(summary).getAllByRole('listitem').map((li) => li.textContent)
    expect(stats).toEqual(['0재실 / 2대', '0PIR 감지', '0RF 감지', '1연결 끊김'])
  })

  it('drops its subscriptions while the tab is hidden and takes them again when it shows', () => {
    resetStore(storeState({ sensors: sensors() }))
    at('/monitor')
    render(<MonitorScreen />)
    pushLive(1)
    setVisibility('hidden')
    expect(useStore.getState().watch).toEqual({})
    expect(useStore.getState().live).toEqual({})
    expect(tileOf('센서 1')).toBeInTheDocument() // the choice is kept
    setVisibility('visible')
    expect(useStore.getState().watch).toEqual({ [deviceId(1)]: 1, [deviceId(3)]: 1 })
  })

  it('folds the case of ?ids= and drops malformed ids, so one typo cannot void the subscription', () => {
    // the server validates a whole live_subscribe frame against the DeviceId pattern
    expect(idsParam(`?ids=${deviceId(1).toUpperCase()}, ${deviceId(2)},not-an-id,${deviceId(1)}`)).toEqual([
      deviceId(1),
      deviceId(2),
    ])
    expect(idsParam('?ids=zz,x')).toBeNull()
  })

  it('a calibrating sensor says the device is moving its thresholds', () => {
    resetStore(storeState({ sensors: [sensor(1, { live: live(1, { busy: 'calibration' }) })] }))
    at('/monitor')
    render(<MonitorScreen />)
    expect(screen.getByText('작업 중 (보정 중)')).toBeInTheDocument()
    expect(screen.getByText('보정 중 — 기기가 임계값을 조정하고 있습니다')).toBeInTheDocument()
  })

  it('unwatches everything on unmount (route change)', () => {
    resetStore(storeState({ sensors: sensors() }))
    at('/monitor')
    const { unmount } = render(<MonitorScreen />)
    pushLive(1)
    unmount()
    expect(useStore.getState().watch).toEqual({})
    expect(useStore.getState().live).toEqual({})
  })

  it('without sessions shows the empty state with the gather link', () => {
    resetStore(storeState({ sensors: [sensor(4, { registry: registry(SITE_A, '창가') })] }))
    at('/monitor')
    render(<MonitorScreen />)
    expect(screen.getByText('연결된 센서가 없습니다')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: '센서 모으기' })).toHaveAttribute('href', '/gather')
  })
})

describe('LiveStrip', () => {
  it('draws PIR, RF and 재실 as the dashboard and monitor do, with the legend', () => {
    resetStore(storeState({ sensors: [sensor(1, { live: live(1) })] }))
    render(<LiveStrip deviceId={deviceId(1)} />)
    pushLive(1)
    expect(screen.getByRole('img', { name: '재실(기기 판정): 재실' }).parentElement).toHaveAttribute('data-tone', 'present')
    expect(screen.getByRole('img', { name: 'PIR: 감지' })).toHaveAttribute('data-tone', 'hit')
    expect(screen.getByRole('img', { name: 'RF(레이더): 감지' })).toHaveAttribute('data-tone', 'hit')
    expect(screen.getByRole('img', { name: 'S1 재실, S2 부재, S3 부재' })).toBeInTheDocument()
    expect(screen.getByLabelText('신호 범례')).toHaveTextContent('빨강 = 원신호 감지 · 파랑 = 기기의 재실 판정')
  })

  it('unwatches while the tab is hidden', () => {
    resetStore(storeState({ sensors: [sensor(1, { live: live(1) })] }))
    render(<LiveStrip deviceId={deviceId(1)} />)
    setVisibility('hidden')
    expect(useStore.getState().watch).toEqual({})
    setVisibility('visible')
    expect(useStore.getState().watch).toEqual({ [deviceId(1)]: 1 })
  })

  it('watches its sensor while mounted, with compact trigger bars only', () => {
    resetStore(storeState({ sensors: [sensor(1, { live: live(1) })] }))
    const { unmount } = render(<LiveStrip deviceId={deviceId(1)} />)
    expect(useStore.getState().watch[deviceId(1)]).toBe(1)
    pushLive(1)
    expect(screen.getAllByRole('img', { name: /^재실 트리거/ })).toHaveLength(6) // Z6 is off
    expect(screen.queryByRole('img', { name: /^재실 유지/ })).not.toBeInTheDocument()
    expect(screen.getByRole('link', { name: '모니터에서 크게 보기' })).toHaveAttribute(
      'href',
      `/monitor?ids=${deviceId(1)}`,
    )
    unmount()
    expect(useStore.getState().watch[deviceId(1)]).toBeUndefined()
  })
})
