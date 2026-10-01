import { fireEvent, render, screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { DashboardScreen } from '../screens/Dashboard'
import { resetStore } from '../store/store'
import { bleName, pending, richSensors, SITE_A, SITE_B, storeState } from './fixtures'

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

  it('the primary action points at the running gather', () => {
    resetStore(storeState({ sensors: richSensors(), gather: { gathering: true, connecting: [] } }))
    render(<DashboardScreen />)
    expect(screen.getByRole('link', { name: '모으는 중 — 보러 가기' })).toBeInTheDocument()
    expect(screen.getAllByText('센서 버튼을 다시 누르세요').length).toBeGreaterThan(0)
  })
})
