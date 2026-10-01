import { act, render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import type { Notice, RegistryInfo } from '../api/types'
import { EditSensorForm } from '../components/EditSensorForm'
import { Notices } from '../components/Notices'
import { GatherScreen } from '../screens/Gather'
import { resetStore, useStore } from '../store/store'
import { t } from '../strings'
import { address, bleName, deviceId, live, NOW_S, registry, SITE_A, SITE_B, sensor, storeState } from './fixtures'

type Call = { path: string; method: string; body: unknown }
type Reply = { status: number; body?: unknown }

const FAIL: Reply = { status: 500, body: { error: { code: 'internal', message: 'boom' } } }

/** Like Gather.test's mock, but a list of replies is used up in order (the last one repeats). */
function mockApi(responses: Record<string, Reply | Reply[]>) {
  const calls: Call[] = []
  vi.stubGlobal(
    'fetch',
    vi.fn(async (path: string, init: RequestInit) => {
      const key = `${init.method} ${path}`
      calls.push({ path, method: init.method ?? 'GET', body: init.body ? JSON.parse(String(init.body)) : undefined })
      const entry = responses[key]
      const r = Array.isArray(entry) ? (entry.length > 1 ? entry.shift() : entry[0]) : entry
      const reply = r ?? { status: 404, body: { error: { code: 'not_found', message: '' } } }
      return new Response(reply.body === undefined ? null : JSON.stringify(reply.body), { status: reply.status })
    }),
  )
  return calls
}

const requests = (calls: Call[]) => calls.map((c) => `${c.method} ${c.path}`)

describe('NameSensorForm saving', () => {
  it('a retry after createSensor failed does not create the new site again', async () => {
    const user = userEvent.setup()
    resetStore(storeState({ sites: [], gather: { gathering: true, connecting: [] }, sensors: [sensor(3, { live: live(3) })] }))
    const calls = mockApi({
      'POST /api/sites': { status: 201, body: { site_id: 'lab-a', name: 'Lab A' } },
      'POST /api/sensors': [FAIL, { status: 201, body: sensor(3, { registry: registry(SITE_A, '센서 1') }) }],
    })
    render(<GatherScreen />)
    const form = screen.getByRole('form', { name: /이름 붙이기/ })
    await user.type(within(form).getByLabelText('새 사이트 이름'), 'Lab A')
    await user.click(within(form).getByRole('button', { name: '저장' }))
    expect(await within(form).findByRole('alert')).toBeInTheDocument()
    await user.click(within(form).getByRole('button', { name: '저장' }))
    expect(await screen.findByText(t.form.saved)).toBeInTheDocument()
    expect(requests(calls)).toEqual(['POST /api/sites', 'POST /api/sensors', 'POST /api/sensors'])
    expect(calls[2]?.body).toMatchObject({ site_id: 'lab-a' })
  })

  it('a "new" site whose name already exists reuses that site', async () => {
    const user = userEvent.setup()
    resetStore(
      storeState({ sites: [SITE_A, SITE_B], gather: { gathering: true, connecting: [] }, sensors: [sensor(3, { live: live(3) })] }),
    )
    const calls = mockApi({ 'POST /api/sensors': { status: 201, body: sensor(3) } })
    render(<GatherScreen />)
    const form = screen.getByRole('form', { name: /이름 붙이기/ })
    await user.selectOptions(within(form).getByLabelText('사이트'), '__new__')
    await user.type(within(form).getByLabelText('새 사이트 이름'), '  lab b ')
    await user.click(within(form).getByRole('button', { name: '저장' }))
    expect(await screen.findByText(t.form.saved)).toBeInTheDocument()
    expect(requests(calls)).toEqual(['POST /api/sensors'])
    expect(calls[0]?.body).toMatchObject({ site_id: 'lab-b' })
  })

  it('a form that goes away with unsaved input (named on another screen) stops blocking new arrivals', async () => {
    const user = userEvent.setup()
    resetStore(storeState({ sites: [SITE_A], gather: { gathering: true, connecting: [] }, sensors: [sensor(2, { live: live(2) })] }))
    render(<GatherScreen />)
    const item = (n: number) => screen.getByText(bleName(n)).closest('li') as HTMLElement
    await user.type(within(item(2)).getByLabelText('위치'), '창가') // unsaved input
    ;(document.activeElement as HTMLElement | null)?.blur()
    act(() => {
      const data = sensor(2, { registry: registry(SITE_A, '문 옆'), live: live(2) })
      useStore.getState().applyMessage({ type: 'sensor', seq: 11, ts: NOW_S, data })
    })
    expect(screen.queryByRole('form')).not.toBeInTheDocument() // now a registered item
    act(() => {
      const data = sensor(3, { live: live(3, { gathered_at: NOW_S + 3 }) })
      useStore.getState().applyMessage({ type: 'sensor', seq: 12, ts: NOW_S, data })
    })
    expect(within(item(3)).getByRole('form')).toBeInTheDocument()
  })
})

describe('EditSensorForm', () => {
  const base: RegistryInfo = registry(SITE_A, '센서 1', { location: '북쪽 벽', notes: '메모' })
  const view = (reg: RegistryInfo) => sensor(1, { registry: reg })

  it('sends only the fields the user changed and follows other screens on the rest', async () => {
    const user = userEvent.setup()
    resetStore(storeState({ sites: [SITE_A, SITE_B] }))
    const calls = mockApi({ [`PATCH /api/sensors/${deviceId(1)}`]: { status: 200, body: view(base) } })
    const { rerender } = render(<EditSensorForm deviceId={deviceId(1)} registry={base} />)
    await user.clear(screen.getByLabelText('이름'))
    await user.type(screen.getByLabelText('이름'), '문 옆')
    // another screen moves the sensor meanwhile
    const remote = { ...base, location: '남쪽 벽' }
    rerender(<EditSensorForm deviceId={deviceId(1)} registry={remote} />)
    expect(screen.getByLabelText('위치')).toHaveValue('남쪽 벽')
    expect(screen.getByLabelText('이름')).toHaveValue('문 옆') // the user's edit stays
    await user.click(screen.getByRole('button', { name: '저장' }))
    expect(await screen.findByText(t.form.saved)).toBeInTheDocument()
    expect(calls.map((c) => c.body)).toEqual([{ alias: '문 옆' }])
  })

  it('a retry after a failed PATCH does not create the new site again', async () => {
    const user = userEvent.setup()
    resetStore(storeState({ sites: [SITE_A] }))
    const calls = mockApi({
      'POST /api/sites': { status: 201, body: { site_id: 'lab-c', name: 'Lab C' } },
      [`PATCH /api/sensors/${deviceId(1)}`]: [FAIL, { status: 200, body: view(base) }],
    })
    render(<EditSensorForm deviceId={deviceId(1)} registry={base} />)
    await user.selectOptions(screen.getByLabelText('사이트'), '__new__')
    await user.type(screen.getByLabelText('새 사이트 이름'), 'Lab C')
    await user.click(screen.getByRole('button', { name: '저장' }))
    expect(await screen.findByText(t.error.internal)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '저장' }))
    expect(await screen.findByText(t.form.saved)).toBeInTheDocument()
    expect(requests(calls)).toEqual([
      'POST /api/sites',
      `PATCH /api/sensors/${deviceId(1)}`,
      `PATCH /api/sensors/${deviceId(1)}`,
    ])
    expect(calls[2]?.body).toEqual({ site_id: 'lab-c' })
  })
})

describe('Notices', () => {
  const notice = (n: number, at: number): Notice => ({
    level: 'warning',
    code: 'gather_failed',
    message: 'timed out',
    device_id: null,
    address: address(n),
    name: bleName(n),
    at,
  })

  it('every toast disappears 8 s after it appeared, whatever arrives later', () => {
    vi.useFakeTimers()
    try {
      resetStore(storeState())
      render(<Notices />)
      const push = (seq: number, n: Notice) =>
        act(() => {
          useStore.getState().applyMessage({ type: 'notice', seq, ts: NOW_S, data: n })
        })
      push(11, notice(1, NOW_S))
      act(() => vi.advanceTimersByTime(3000))
      push(12, notice(2, NOW_S + 3))
      expect(screen.getByText(new RegExp(bleName(1)))).toBeInTheDocument()
      act(() => vi.advanceTimersByTime(5000)) // 8 s after the first
      expect(screen.queryByText(new RegExp(bleName(1)))).not.toBeInTheDocument()
      expect(screen.getByText(new RegExp(bleName(2)))).toBeInTheDocument()
      act(() => vi.advanceTimersByTime(3000))
      expect(screen.queryByText(new RegExp(bleName(2)))).not.toBeInTheDocument()
    } finally {
      vi.useRealTimers()
    }
  })
})
