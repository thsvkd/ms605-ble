import { act, render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import { GatherScreen } from '../screens/Gather'
import { resetStore, useStore } from '../store/store'
import { address, bleName, deviceId, live, NOW_S, registry, SITE_A, sensor, storeState } from './fixtures'

type Call = { path: string; method: string; body: unknown }

function mockApi(responses: Record<string, { status: number; body?: unknown }>) {
  const calls: Call[] = []
  vi.stubGlobal(
    'fetch',
    vi.fn(async (path: string, init: RequestInit) => {
      calls.push({ path, method: init.method ?? 'GET', body: init.body ? JSON.parse(String(init.body)) : undefined })
      const r = responses[`${init.method} ${path}`] ?? { status: 404, body: { error: { code: 'not_found', message: '' } } }
      return new Response(r.body === undefined ? null : JSON.stringify(r.body), { status: r.status })
    }),
  )
  return calls
}

describe('GatherScreen', () => {
  it('idle: asks to start gathering', () => {
    resetStore(storeState())
    render(<GatherScreen />)
    expect(screen.getByRole('heading', { name: '센서 모으기를 시작하세요' })).toBeInTheDocument()
    expect(screen.getByText('시작한 뒤 센서의 버튼을 누르면 여기에 나타납니다')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '센서 모으기 시작' })).toBeInTheDocument()
    expect(screen.getByText('시뮬레이터')).toBeInTheDocument() // server.sim is set in the fixture
    expect(screen.getByText('목록 파일 가져오기 (yaml)')).toBeInTheDocument()
  })

  it('gathering, nothing gathered yet: the big "press the button" state', () => {
    resetStore(storeState({ gather: { gathering: true, connecting: [{ address: address(1), since: NOW_S }] } }))
    render(<GatherScreen />)
    expect(screen.getByRole('heading', { name: '센서 버튼을 누르세요' })).toBeInTheDocument()
    expect(screen.getByText('누른 센서가 아래에 나타납니다')).toBeInTheDocument()
    expect(screen.getByText('센서 1대 연결 중…')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '모으기 끝내기' })).toBeInTheDocument()
  })

  it('gathering with sensors: compact hero, list, registered sensors show their alias', () => {
    resetStore(
      storeState({
        gather: { gathering: true, connecting: [] },
        sensors: [
          sensor(1, { registry: registry(SITE_A, '센서 1'), live: live(1) }),
          sensor(2, { live: live(2) }),
        ],
      }),
    )
    render(<GatherScreen />)
    expect(screen.queryByRole('heading', { name: '센서 버튼을 누르세요' })).not.toBeInTheDocument()
    expect(screen.getByRole('status', { name: '' })).toHaveTextContent('센서 버튼을 누르세요')
    const list = screen.getByRole('region', { name: '이번에 모은 센서 (2)' })
    expect(within(list).getByText('센서 1')).toBeInTheDocument()
    expect(within(list).getByText('등록됨')).toBeInTheDocument()
    expect(within(list).getByText(bleName(2))).toBeInTheDocument()
    expect(within(list).getByText('새 센서')).toBeInTheDocument()
    // every item starts folded: no form until the operator asks for one
    expect(within(list).queryByRole('form')).not.toBeInTheDocument()
    expect(within(list).getByRole('button', { name: '이름 붙이기' })).toBeInTheDocument()
  })

  it('saving a new sensor into a new site calls createSite then createSensor', async () => {
    const user = userEvent.setup()
    const identity = live(3, { address: '02:00:00:ab:cd:ef' })
    resetStore(storeState({ sites: [], gather: { gathering: true, connecting: [] }, sensors: [sensor(3, { live: identity })] }))
    const calls = mockApi({
      'POST /api/sites': { status: 201, body: { site_id: 'lab-a', name: 'Lab A' } },
      'POST /api/sensors': { status: 201, body: sensor(3, { registry: registry(SITE_A, 'MS605-ABCDEF'), live: identity }) },
    })
    render(<GatherScreen />)
    await user.click(screen.getByRole('button', { name: '이름 붙이기' }))
    const form = screen.getByRole('form', { name: /이름 붙이기/ })
    expect(within(form).getByLabelText('이름')).toHaveValue('MS605-ABCDEF')
    await user.type(within(form).getByLabelText('새 사이트 이름'), 'Lab A')
    await user.type(within(form).getByLabelText('위치'), '북쪽 벽')
    await user.click(within(form).getByRole('button', { name: '저장' }))

    expect(calls.map((c) => `${c.method} ${c.path}`)).toEqual(['POST /api/sites', 'POST /api/sensors'])
    expect(calls[0]?.body).toEqual({ name: 'Lab A', site_id: null })
    expect(calls[1]?.body).toEqual({
      device_id: deviceId(3),
      site_id: 'lab-a',
      alias: 'MS605-ABCDEF',
      location: '북쪽 벽',
      notes: '',
    })
    expect(await screen.findByText('등록했습니다')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '저장' })).not.toBeInTheDocument()
    expect(localStorage.getItem('ms605.lastSiteId')).toBe('lab-a')
  })

  it('a 409 says another screen registered it first and folds the form', async () => {
    const user = userEvent.setup()
    resetStore(storeState({ sites: [SITE_A], gather: { gathering: true, connecting: [] }, sensors: [sensor(4, { live: live(4) })] }))
    const calls = mockApi({
      'POST /api/sensors': { status: 409, body: { error: { code: 'already_exists', message: 'exists' } } },
    })
    render(<GatherScreen />)
    await user.click(screen.getByRole('button', { name: '이름 붙이기' }))
    const form = screen.getByRole('form', { name: /이름 붙이기/ })
    expect(within(form).getByLabelText('사이트')).toHaveValue('lab-a') // the only site is the default
    await user.click(within(form).getByRole('button', { name: '저장' }))
    expect(calls.map((c) => c.path)).toEqual(['/api/sensors'])
    expect(await screen.findByText('다른 화면에서 이미 등록했습니다')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '저장' })).not.toBeInTheDocument()
  })

  it('an empty alias is rejected inline without a request', async () => {
    const user = userEvent.setup()
    resetStore(storeState({ sites: [SITE_A], gather: { gathering: true, connecting: [] }, sensors: [sensor(4, { live: live(4) })] }))
    const calls = mockApi({})
    render(<GatherScreen />)
    await user.click(screen.getByRole('button', { name: '이름 붙이기' }))
    await user.clear(screen.getByLabelText('이름'))
    await user.click(screen.getByRole('button', { name: '저장' }))
    expect(screen.getByLabelText('이름')).toHaveAccessibleDescription('입력해 주세요')
    expect(calls).toHaveLength(0)
  })

  describe('arrivals', () => {
    const arrive = (n: number, seq: number) =>
      act(() => {
        useStore.getState().applyMessage({ type: 'sensor', seq, ts: NOW_S, data: sensor(n, { live: live(n, { gathered_at: NOW_S + n }) }) })
      })
    const item = (n: number) => screen.getByText(bleName(n)).closest('li') as HTMLElement

    it('only sensors after the first snapshot are highlighted; every item stays folded', () => {
      resetStore(storeState({ sites: [SITE_A], gather: { gathering: true, connecting: [] }, sensors: [sensor(2, { live: live(2) })] }))
      render(<GatherScreen />)
      expect(item(2).className).not.toMatch(/fresh/)
      expect(within(item(2)).queryByRole('form')).not.toBeInTheDocument()

      arrive(3, 11)
      expect(item(3).className).toMatch(/fresh/)
      expect(within(item(3)).queryByRole('form')).not.toBeInTheDocument()
      expect(within(item(2)).queryByRole('form')).not.toBeInTheDocument()
    })
  })
})
