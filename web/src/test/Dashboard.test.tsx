import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { DashboardScreen } from '../screens/Dashboard'
import { resetStore, useStore } from '../store/store'
import {
  bleName,
  deviceId,
  jobAllVerified,
  jobRunning,
  live,
  liveData,
  NOW_S,
  pending,
  richSensors,
  SITE_A,
  SITE_B,
  storeState,
} from './fixtures'

describe('DashboardScreen', () => {
  it('draws sites, sensors, the unregistered and the pending entries', () => {
    resetStore(storeState({ sites: [SITE_A, SITE_B], sensors: richSensors(), pending: [pending(SITE_A, '센서 9', 9)] }))
    render(<DashboardScreen />)

    const summary = screen.getByRole('list', { name: '센서 현황' })
    expect(within(summary).getByText('연결됨 2')).toBeInTheDocument()
    expect(within(summary).getByText('끊김 1')).toBeInTheDocument()
    expect(within(summary).getByText('연결 안 됨 2')).toBeInTheDocument()
    expect(within(summary).getByText('전체 5')).toBeInTheDocument()

    const labA = screen.getByRole('region', { name: /Lab A/ })
    expect(within(labA).getByText('센서 1')).toBeInTheDocument()
    expect(within(labA).getByText('북쪽 벽')).toBeInTheDocument()
    expect(within(labA).getByText('센서 모으기를 켜고 버튼을 누르세요')).toBeInTheDocument() // lost hint, not gathering
    expect(within(labA).getByText('배터리 64% (마지막 값)')).toBeInTheDocument()
    expect(screen.getByRole('region', { name: /Lab B/ })).toHaveTextContent('창가 센서')

    const unregistered = screen.getByRole('region', { name: /등록되지 않은 센서/ })
    expect(within(unregistered).getByText(bleName(5))).toBeInTheDocument()
    expect(within(unregistered).getByRole('button', { name: '이름 붙이기' })).toBeInTheDocument()

    const pend = screen.getByRole('region', { name: /가져온 센서/ })
    expect(within(pend).getByText('센서 9')).toBeInTheDocument()
    expect(within(pend).getByText('버튼을 누르면 자동으로 등록됩니다')).toBeInTheDocument()

    expect(screen.getByRole('link', { name: '센서 모으기' })).toHaveAttribute('href', '/gather')
    expect(screen.getByRole('button', { name: '모두 연결 해제' })).toBeInTheDocument()
  })

  it('every status shows text, not only colour', () => {
    resetStore(storeState({ sites: [SITE_A, SITE_B], sensors: richSensors() }))
    render(<DashboardScreen />)
    expect(screen.getAllByText('연결됨').length).toBeGreaterThan(0)
    expect(screen.getByText('연결 끊김')).toBeInTheDocument()
    expect(screen.getAllByText('연결 안 됨').length).toBe(2)
  })

  it('shows the empty state with the gather action', () => {
    resetStore(storeState())
    render(<DashboardScreen />)
    expect(screen.getByText('아직 등록된 센서가 없습니다')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: '센서 모으기' })).toHaveAttribute('href', '/gather')
    expect(screen.queryByRole('button', { name: '모두 연결 해제' })).not.toBeInTheDocument()
  })

  it('warns that releasing everything also stops a running gather', () => {
    resetStore(storeState({ sensors: richSensors(), gather: { gathering: true, connecting: [] } }))
    render(<DashboardScreen />)
    fireEvent.click(screen.getByRole('button', { name: '모두 연결 해제' }))
    expect(screen.getByText(/센서 모으기를 멈추고 모든 연결을 해제합니다/)).toBeInTheDocument()
  })

  it('release-all is disabled with a note while an apply job runs, and enabled once it is done', () => {
    resetStore(storeState({ sensors: richSensors(), apply: jobRunning() }))
    render(<DashboardScreen />)
    const button = screen.getByRole('button', { name: '모두 연결 해제' })
    expect(button).toBeDisabled()
    expect(button).toHaveAccessibleDescription('설정 적용이 끝난 뒤 해제할 수 있습니다')
    cleanup()
    resetStore(storeState({ sensors: richSensors(), apply: jobAllVerified() }))
    render(<DashboardScreen />)
    expect(screen.getByRole('button', { name: '모두 연결 해제' })).toBeEnabled()
    expect(screen.queryByText('설정 적용이 끝난 뒤 해제할 수 있습니다')).not.toBeInTheDocument()
  })

  it('the primary action points at the running gather', () => {
    resetStore(storeState({ sensors: richSensors(), gather: { gathering: true, connecting: [] } }))
    render(<DashboardScreen />)
    expect(screen.getByRole('link', { name: '모으는 중 — 보러 가기' })).toBeInTheDocument()
    expect(screen.getAllByText('센서 버튼을 다시 누르세요').length).toBeGreaterThan(0)
  })
})

describe('DashboardScreen live presence (14.8.5.1)', () => {
  const setVisibility = (state: DocumentVisibilityState) => {
    Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => state })
    act(() => {
      document.dispatchEvent(new Event('visibilitychange'))
    })
  }
  const pushLive = (n: number, patch: Parameters<typeof liveData>[1] = {}) =>
    act(() => {
      useStore.getState().applyMessage({ type: 'live', seq: null, ts: NOW_S, data: liveData(n, patch) })
    })

  afterEach(() => {
    Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'visible' })
  })

  it('watches registered, connected sensors only while the tab is visible', () => {
    resetStore(storeState({ sites: [SITE_A, SITE_B], sensors: richSensors() }))
    const { unmount } = render(<DashboardScreen />)
    // sensor 5 is connected but unregistered (no card); sensor 2 is lost
    expect(useStore.getState().watch).toEqual({ [deviceId(1)]: 1 })

    pushLive(1)
    setVisibility('hidden')
    expect(useStore.getState().watch).toEqual({})
    expect(useStore.getState().live).toEqual({})
    setVisibility('visible')
    expect(useStore.getState().watch).toEqual({ [deviceId(1)]: 1 })

    unmount()
    expect(useStore.getState().watch).toEqual({})
  })

  it('unwatches a sensor that disconnects', () => {
    resetStore(storeState({ sites: [SITE_A, SITE_B], sensors: richSensors() }))
    render(<DashboardScreen />)
    act(() => {
      const { sensors } = useStore.getState()
      const s = sensors[deviceId(1)]
      if (s) useStore.setState({ sensors: { ...sensors, [deviceId(1)]: { ...s, live: live(1, { link: 'disconnected' }) } } })
    })
    expect(useStore.getState().watch).toEqual({})
  })

  it('a sensor joining or leaving never touches the other cards\' subscriptions or frames', () => {
    resetStore(storeState({ sites: [SITE_A, SITE_B], sensors: richSensors() }))
    render(<DashboardScreen />)
    const setLink = (n: number, link: 'connected' | 'disconnected') =>
      act(() => {
        const { sensors } = useStore.getState()
        const s = sensors[deviceId(n)]
        if (s) useStore.setState({ sensors: { ...sensors, [deviceId(n)]: { ...s, live: live(n, { link }) } } })
      })
    setLink(4, 'connected') // two connected cards: sensors 1 and 4
    expect(useStore.getState().watch).toEqual({ [deviceId(1)]: 1, [deviceId(4)]: 1 })
    pushLive(1)

    // record every watch map the store goes through: sensor 1 must never drop to 0
    const seen: Record<string, number>[] = []
    const stop = useStore.subscribe((s) => seen.push(s.watch))
    setLink(3, 'connected')
    setLink(4, 'disconnected')
    stop()
    expect(seen.length).toBeGreaterThan(0)
    expect(seen.every((w) => w[deviceId(1)] === 1)).toBe(true)
    expect(useStore.getState().watch).toEqual({ [deviceId(1)]: 1, [deviceId(3)]: 1 })
    expect(useStore.getState().live[deviceId(1)]).toBeDefined() // card 1 keeps its frame, no '?' blink
    const card = screen.getByRole('link', { name: /^센서 1/ })
    expect(within(card).getByRole('img', { name: '재실(기기 판정): 재실' })).toBeInTheDocument()
  })

  it('shows the device 재실 call as the card answer, apart from the raw PIR and RF chips', () => {
    resetStore(storeState({ sites: [SITE_A, SITE_B], sensors: richSensors() }))
    render(<DashboardScreen />)
    expect(screen.getByLabelText('신호 범례')).toHaveTextContent('PIR 포함 여부 미확인')

    const card = screen.getByRole('link', { name: /^센서 1/ })
    const unknown = within(card).getByRole('img', { name: '재실(기기 판정): 알 수 없음, 아직 받은 값 없음' })
    expect(unknown).toHaveAttribute('title', '재실(기기 판정): 알 수 없음, 아직 받은 값 없음')
    expect(within(card).getByRole('img', { name: 'PIR: 알 수 없음, 아직 받은 값 없음' })).toHaveAttribute(
      'data-tone',
      'unknown',
    )

    pushLive(1)
    const verdict = within(card).getByRole('img', { name: '재실(기기 판정): 재실' })
    expect(verdict).toHaveTextContent('재실')
    expect(verdict.parentElement).toHaveAttribute('data-tone', 'present')
    expect(within(card).getByText('기기 판정')).toHaveAttribute('title', '기기 판정 · PIR 포함 여부 미확인')
    expect(within(card).getByRole('img', { name: 'PIR: 감지' })).toHaveTextContent('PIR 감지')
    expect(within(card).getByRole('img', { name: 'RF(레이더): 감지' })).toHaveAttribute('data-tone', 'hit')
    expect(within(card).getByRole('img', { name: 'S1 재실, S2 부재, S3 부재' })).toBeInTheDocument()

    // raw signals firing without the device's call stays 부재: nothing is computed on the host
    pushLive(1, { sub_sensor_presence: [false, false, false] })
    expect(within(card).getByRole('img', { name: '재실(기기 판정): 부재' })).toHaveTextContent('부재')
    expect(within(card).getByRole('img', { name: 'PIR: 감지' })).toBeInTheDocument()

    // a lost sensor's card has no live block
    const lost = screen.getByRole('link', { name: /^센서 2/ })
    expect(within(lost).queryByRole('img', { name: /재실\(기기 판정\)/ })).not.toBeInTheDocument()
  })
})
