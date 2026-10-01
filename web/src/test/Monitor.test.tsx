import { act, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { LiveStrip } from '../components/LiveStrip'
import { Meter } from '../components/Meter'
import { meter } from '../meter'
import { idsParam, MonitorScreen } from '../screens/Monitor'
import { resetStore, useStore } from '../store/store'
import { deviceId, live, liveData, NOW_S, registry, SITE_A, sensor, storeState } from './fixtures'

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

describe('MonitorScreen', () => {
  const sensors = () => [
    sensor(1, { registry: registry(SITE_A, '센서 1', { location: '북쪽 벽' }), live: live(1) }),
    sensor(2, { registry: registry(SITE_A, '센서 2'), live: live(2, { link: 'lost', lost_reason: 'idle' }) }),
    sensor(3, { registry: registry(SITE_A, '센서 3'), live: live(3) }),
  ]

  const rowOf = (name: string) => {
    const row = screen.getByRole('rowheader', { name: new RegExp(name) }).closest('tr')
    if (!row) throw new Error(`no row for ${name}`)
    return row
  }

  it('starts with every connected sensor: one row each, Z0..Z6 as columns, a summary and the legend', () => {
    resetStore(storeState({ sensors: sensors() }))
    at('/monitor')
    render(<MonitorScreen />)
    expect(useStore.getState().watch).toEqual({ [deviceId(1)]: 1, [deviceId(3)]: 1 })
    expect(screen.getByRole('button', { name: '센서 1' })).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByRole('button', { name: '센서 2' })).toHaveAttribute('aria-pressed', 'false')
    expect(screen.getByText(/┃ = 임계값\(고정\)/)).toBeInTheDocument()
    expect(screen.getByText('기기 판정 (S1~S3 중 하나라도) · PIR 포함 여부 미확인')).toBeInTheDocument()

    const region = screen.getByRole('region', { name: '존 표, 옆으로 스크롤' })
    expect(region).toHaveAttribute('tabindex', '0') // the sideways scroll is reachable from the keyboard
    expect(screen.getByRole('table', { name: '센서별 존 실시간 값' })).toBeInTheDocument() // not the region's words again
    const heads = within(screen.getByRole('table')).getAllByRole('columnheader')
    expect(heads.map((h) => h.textContent)).toEqual(['센서', 'PIR · RF · 재실', 'Z0', 'Z1', 'Z2', 'Z3', 'Z4', 'Z5', 'Z6'])

    const row = rowOf('센서 1')
    // the row header (repeated before every cell) is the name and state only; the signals have their own cell
    const header = within(row).getByRole('rowheader')
    expect(header).toHaveAccessibleName(/^센서 1.*북쪽 벽.*데이터 수신 대기 중…$/)
    expect(header).not.toHaveAccessibleName(/PIR|RF|재실|S1/)
    expect(within(row).getByText('데이터 수신 대기 중…')).toBeInTheDocument()
    expect(within(row).getByRole('img', { name: 'PIR: 알 수 없음, 아직 받은 값 없음' })).toBeInTheDocument()
    expect(within(row).getByRole('img', { name: '재실(기기 판정): 알 수 없음, 아직 받은 값 없음' })).toBeInTheDocument()

    pushLive(1)
    expect(screen.getByRole('columnheader', { name: /Z0/ })).toHaveTextContent('Z00.8 m')
    expect(within(row).getByRole('rowheader')).toHaveAccessibleName(/^센서 1.*북쪽 벽$/)
    const [signals, ...cells] = within(row).getAllByRole('cell')
    expect(cells).toHaveLength(7)
    expect(within(signals as HTMLElement).getAllByRole('img')).toHaveLength(4) // PIR, RF, 재실, S1..S3
    const [z0, , z2, z3, , , z6] = cells as [HTMLElement, HTMLElement, HTMLElement, HTMLElement, ...HTMLElement[]]
    expect(z3).toHaveAttribute('title', 'Z3 · 3.2 m')
    expect(z3).not.toHaveTextContent('3.2 m') // the shared distance is in the column head, not in every cell
    expect(z6).toHaveTextContent('꺼짐')
    expect(z0).toHaveAttribute('data-over', 'trigger')
    expect(z3).not.toHaveAttribute('data-over')
    expect(within(z0).getByRole('img', { name: '재실 트리거 64, 임계값 60, 초과' })).toBeInTheDocument()
    expect(within(z0).getByRole('img', { name: '재실 유지 22, 임계값 30, 이하' })).toBeInTheDocument()
    expect(within(z2).getByRole('img', { name: '재실 트리거 -10, 임계값 -33, 초과' })).toBeInTheDocument()
    expect(within(row).getByRole('img', { name: 'PIR: 감지' })).toHaveAttribute('data-tone', 'hit')
    expect(within(row).getByRole('img', { name: 'RF(레이더): 감지' })).toHaveAttribute('data-tone', 'hit')
    expect(within(row).getByRole('img', { name: '재실(기기 판정): 재실' })).toHaveAttribute('data-tone', 'present')
    expect(within(row).getByRole('img', { name: 'S1 재실, S2 부재, S3 부재' })).toBeInTheDocument()
    expect(row).toHaveAttribute('data-present', 'true')

    const summary = screen.getByRole('list', { name: '지금 감지 요약' })
    const stats = within(summary).getAllByRole('listitem').map((li) => li.textContent)
    expect(stats).toEqual(['1재실 / 2대', '1PIR 감지', '1RF 감지', '1값 없음']) // sensor 3 has no frame yet
  })

  it('takes ?ids=, shows a lost sensor dimmed with its hint, and unwatches a chip turned off', () => {
    resetStore(storeState({ sensors: sensors() }))
    at(`/monitor?ids=${deviceId(1)},${deviceId(2)}`)
    render(<MonitorScreen />)
    const lost = rowOf('센서 2')
    expect(lost).toHaveAttribute('data-stale', 'true')
    expect(within(lost).getByText('센서 모으기를 켜고 버튼을 누르세요')).toBeInTheDocument()
    act(() => {
      useStore.getState().applyMessage({ type: 'live', seq: null, ts: NOW_S, data: liveData(2) })
    })
    expect(within(lost).getByText('실시간 값 없음 (마지막 값)')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '센서 2' }))
    expect(useStore.getState().watch).toEqual({ [deviceId(1)]: 1 })
    expect(screen.queryByRole('rowheader', { name: /센서 2/ })).not.toBeInTheDocument()
    expect(JSON.parse(localStorage.getItem('ms605.monitorIds') ?? '[]')).toEqual([deviceId(1)])

    fireEvent.click(screen.getByRole('button', { name: '모두 해제' }))
    expect(useStore.getState().watch).toEqual({})
    expect(screen.getByText('볼 센서를 고르세요')).toBeInTheDocument()
    expect(screen.queryByRole('table')).not.toBeInTheDocument()
  })

  it('the summary never counts a lost sensor\'s last frame as a detection now', () => {
    resetStore(storeState({ sensors: sensors() }))
    at(`/monitor?ids=${deviceId(1)},${deviceId(2)}`)
    render(<MonitorScreen />)
    pushLive(1, { pir: false, sub_sensor_presence: [false, false, false], zones: [] })
    pushLive(2) // lost sensor 2: its last frame says 재실, PIR and RF
    const lost = rowOf('센서 2')
    expect(within(lost).getByText('실시간 값 없음 (마지막 값)')).toBeInTheDocument()
    expect(lost).toHaveAttribute('data-present', 'false') // no blue 재실 rail on history

    const summary = screen.getByRole('list', { name: '지금 감지 요약' })
    const stats = within(summary).getAllByRole('listitem').map((li) => li.textContent)
    expect(stats).toEqual(['0재실 / 2대', '0PIR 감지', '0RF 감지', '1연결 끊김'])
  })

  it('puts zone distances in the column heads only when every watched sensor agrees', () => {
    resetStore(storeState({ sensors: sensors() }))
    at('/monitor')
    render(<MonitorScreen />)
    pushLive(1)
    expect(screen.getByRole('columnheader', { name: /Z3/ })).toHaveTextContent('Z33.2 m')

    // sensor 3 is configured with other zone spacing: no head can speak for both rows
    pushLive(3, { zones: liveData(3).zones.map((z) => ({ ...z, distance_m: 0.6 * (z.index + 1) })) })
    expect(screen.getByRole('columnheader', { name: /Z3/ })).toHaveTextContent(/^Z3$/)
    const z3 = (name: string) => within(rowOf(name)).getAllByRole('cell')[4] as HTMLElement // [signals, Z0..]
    expect(z3('센서 1')).toHaveTextContent('3.2 m')
    expect(z3('센서 3')).toHaveTextContent('2.4 m')
    expect(z3('센서 3')).toHaveAttribute('title', 'Z3 · 2.4 m')
  })

  it('drops its subscriptions while the tab is hidden and takes them again when it shows', () => {
    resetStore(storeState({ sensors: sensors() }))
    at('/monitor')
    render(<MonitorScreen />)
    pushLive(1)
    setVisibility('hidden')
    expect(useStore.getState().watch).toEqual({})
    expect(useStore.getState().live).toEqual({})
    expect(rowOf('센서 1')).toBeInTheDocument() // the choice is kept
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
