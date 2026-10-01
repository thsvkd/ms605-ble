import { act, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { LiveStrip } from '../components/LiveStrip'
import { Meter } from '../components/Meter'
import { meter } from '../meter'
import { idsParam, MonitorScreen } from '../screens/Monitor'
import { resetStore, useStore } from '../store/store'
import { deviceId, live, liveData, NOW_S, registry, SITE_A, sensor, storeState } from './fixtures'

const at = (path: string) => window.history.replaceState(null, '', path)
const pushLive = (n: number) =>
  act(() => {
    useStore.getState().applyMessage({ type: 'live', seq: null, ts: NOW_S, data: liveData(n) })
  })

afterEach(() => at('/'))

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

describe('MonitorScreen', () => {
  const sensors = () => [
    sensor(1, { registry: registry(SITE_A, '센서 1', { location: '북쪽 벽' }), live: live(1) }),
    sensor(2, { registry: registry(SITE_A, '센서 2'), live: live(2, { link: 'lost', lost_reason: 'idle' }) }),
    sensor(3, { registry: registry(SITE_A, '센서 3'), live: live(3) }),
  ]

  it('starts with every connected sensor and draws seven zone rows per card', () => {
    resetStore(storeState({ sensors: sensors() }))
    at('/monitor')
    render(<MonitorScreen />)
    expect(useStore.getState().watch).toEqual({ [deviceId(1)]: 1, [deviceId(3)]: 1 })
    expect(screen.getByRole('button', { name: '센서 1' })).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByRole('button', { name: '센서 2' })).toHaveAttribute('aria-pressed', 'false')
    expect(screen.getByText(/┃ = 임계값\(고정\)/)).toBeInTheDocument()

    const card = screen.getByRole('region', { name: /센서 1/ })
    expect(within(card).getByText('데이터 수신 대기 중…')).toBeInTheDocument()
    pushLive(1)
    expect(within(card).getAllByText(/^Z\d · /)).toHaveLength(7)
    expect(within(card).getByText('꺼짐')).toBeInTheDocument()
    expect(within(card).getByText('PIR 감지')).toBeInTheDocument()
    expect(within(card).getByText('S1 재실')).toBeInTheDocument()
    expect(within(card).getByText('S2 부재')).toBeInTheDocument()
    expect(within(card).getByRole('img', { name: '재실 트리거 64, 임계값 60, 초과' })).toBeInTheDocument()
    expect(within(card).getByRole('img', { name: '재실 트리거 -10, 임계값 -33, 초과' })).toBeInTheDocument()
  })

  it('takes ?ids=, shows a lost sensor dimmed with its hint, and unwatches a chip turned off', () => {
    resetStore(storeState({ sensors: sensors() }))
    at(`/monitor?ids=${deviceId(1)},${deviceId(2)}`)
    render(<MonitorScreen />)
    const lost = screen.getByRole('region', { name: /센서 2/ })
    expect(lost).toHaveAttribute('data-stale', 'true')
    expect(within(lost).getByText('센서 모으기를 켜고 버튼을 누르세요')).toBeInTheDocument()
    expect(within(lost).getByText('실시간 값 없음 (마지막 값)')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '센서 2' }))
    expect(useStore.getState().watch).toEqual({ [deviceId(1)]: 1 })
    expect(screen.queryByRole('region', { name: /센서 2/ })).not.toBeInTheDocument()
    expect(JSON.parse(localStorage.getItem('ms605.monitorIds') ?? '[]')).toEqual([deviceId(1)])

    fireEvent.click(screen.getByRole('button', { name: '모두 해제' }))
    expect(useStore.getState().watch).toEqual({})
    expect(screen.getByText('볼 센서를 고르세요')).toBeInTheDocument()
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
