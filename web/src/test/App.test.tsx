import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { App } from '../App'
import { resetStore } from '../store/store'
import { storeState } from './fixtures'

describe('App', () => {
  it('shows AuthRequired instead of everything when unauthorized', () => {
    resetStore({ ...storeState(), conn: 'unauthorized' })
    render(<App />)
    expect(screen.getByRole('heading', { name: '접속 권한이 없습니다' })).toBeInTheDocument()
    expect(screen.getByText(/접근 토큰이 바뀌었거나 저장 위치가 달라졌다면 새 주소가 필요합니다/)).toBeInTheDocument()
    expect(screen.queryByRole('navigation')).not.toBeInTheDocument()
  })

  it('renders the shell with navigation and the sim badge', () => {
    resetStore(storeState({ gather: { gathering: true, connecting: [] } }))
    render(<App />)
    expect(screen.getAllByRole('navigation', { name: '주 메뉴' }).length).toBeGreaterThan(0)
    expect(screen.getByText('시뮬레이터')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /추가 중/ })).toHaveAttribute('href', '/gather')
  })

  it('shows the reconnect banner and dims the page while the socket is down', () => {
    resetStore({ ...storeState(), conn: 'reconnecting', failures: 2 })
    render(<App />)
    expect(screen.getByText('서버에 연결할 수 없습니다')).toBeInTheDocument()
  })
})
