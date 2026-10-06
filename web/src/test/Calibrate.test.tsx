import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { PresenceView } from '../api/types'
import { CalibrationPill } from '../components/CalibrationPill'
import { CalibrateScreen } from '../screens/Calibrate'
import { SensorDetailScreen } from '../screens/SensorDetail'
import { resetStore, useStore } from '../store/store'
import {
  BATCH_ID,
  batchView,
  calibSensors,
  deviceId,
  job,
  live,
  NOW_S,
  snapshotMsg,
  storeState,
  succeededJob,
} from './fixtures'

type Call = { path: string; method: string; body: unknown }

function mockApi(responses: Record<string, { status: number; body?: unknown }>) {
  const calls: Call[] = []
  vi.stubGlobal(
    'fetch',
    vi.fn(async (path: string, init: RequestInit) => {
      calls.push({ path, method: init.method ?? 'GET', body: init.body ? JSON.parse(String(init.body)) : undefined })
      const r = responses[`${init.method} ${path}`] ?? {
        status: 404,
        body: { error: { code: 'not_found', message: '' } },
      }
      return new Response(r.body === undefined ? null : JSON.stringify(r.body), { status: r.status })
    }),
  )
  return calls
}

const presence = (n: number, occupied: boolean): PresenceView => ({
  device_id: deviceId(n),
  samples: 3,
  presence: occupied,
  pir: occupied,
  occupied,
  error: null,
})

const ids = [1, 2, 3].map(deviceId)
const at = (path: string) => window.history.replaceState(null, '', path)
afterEach(() => {
  at('/')
  sessionStorage.clear()
})

async function toPreflight(occupiedSensor2: boolean) {
  const calls = mockApi({
    'POST /api/preflight': {
      status: 200,
      body: { checked_at: NOW_S, results: [presence(1, false), presence(2, occupiedSensor2), presence(3, false)] },
    },
    'POST /api/batches': { status: 202, body: batchView({ state: 'waiting' }) },
  })
  resetStore(storeState({ sensors: calibSensors() }))
  render(<CalibrateScreen />)
  fireEvent.click(screen.getByRole('button', { name: '연결된 센서 모두 선택' }))
  fireEvent.click(screen.getByRole('button', { name: '다음: 사전점검 (3대)' }))
  expect(screen.getByText('사람이 있는지 확인하는 중… (약 3초)')).toBeInTheDocument()
  await screen.findAllByText(occupiedSensor2 ? '아직 사람 있음' : '비어 있음')
  return calls
}

describe('CalibrateScreen: select -> preflight -> start', () => {
  it('blocks a "now" start on an occupied room until the operator overrides', async () => {
    const calls = await toPreflight(true)
    expect(calls[0]).toEqual({ path: '/api/preflight', method: 'POST', body: { device_ids: ids } })
    expect(screen.getByRole('alert')).toHaveTextContent('1대에서 아직 사람이 감지됩니다')
    expect(screen.getByText('근거: 센서 재실·PIR 감지')).toBeInTheDocument()
    const startBtn = screen.getByRole('button', { name: '보정 시작' })
    expect(startBtn).toBeDisabled()
    fireEvent.click(screen.getByRole('checkbox', { name: '사람이 없는 것을 확인했습니다 (경고 무시하고 시작)' }))
    expect(startBtn).toBeEnabled()
    fireEvent.click(startBtn)
    await waitFor(() => expect(calls).toHaveLength(2))
    expect(calls[1]).toEqual({
      path: '/api/batches',
      method: 'POST',
      body: { device_ids: ids, start: 'now', presence_override: true },
    })
  })

  it('a delayed start needs no override and sends delay_s', async () => {
    const calls = await toPreflight(true)
    fireEvent.click(screen.getByRole('radio', { name: 'N초 후' }))
    expect(screen.queryByRole('checkbox', { name: /경고 무시하고 시작/ })).not.toBeInTheDocument()
    expect(screen.getByText('시작 전에 방을 비워 주세요. 카운트다운이 끝나면 보정이 시작됩니다')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '30초 후 보정 시작' }))
    await waitFor(() => expect(calls).toHaveLength(2))
    expect(calls[1]?.body).toEqual({ device_ids: ids, start: 'delay', delay_s: 30, presence_override: false })
  })

  it('a scheduled start sends an absolute time', async () => {
    const calls = await toPreflight(false)
    fireEvent.click(screen.getByRole('radio', { name: '시각 예약' }))
    expect(screen.getByText(/예약 시각까지 센서 연결을 유지합니다/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /에 보정 예약$/ }))
    await waitFor(() => expect(calls).toHaveLength(2))
    const body = calls[1]?.body as { start: string; at: string }
    expect(body.start).toBe('at')
    expect(Date.parse(body.at)).toBeGreaterThan(Date.now())
  })

  it('a sensor that is not connected or busy cannot be picked, and says why', () => {
    resetStore(
      storeState({
        sensors: calibSensors((n) =>
          n === 2 ? { live: live(2, { link: 'lost' }) } : n === 3 ? { live: live(3, { busy: 'read' }) } : {},
        ),
      }),
    )
    render(<CalibrateScreen />)
    expect(screen.getByRole('checkbox', { name: /센서 2/ })).toBeDisabled()
    expect(screen.getByText('연결되어 있지 않음')).toBeInTheDocument()
    expect(screen.getByRole('checkbox', { name: /센서 3/ })).toBeDisabled()
    expect(screen.getAllByText('작업 중 (읽는 중)').length).toBeGreaterThan(0)
    expect(screen.getByRole('button', { name: '다음: 사전점검 (0대)' })).toBeDisabled()
  })

  it('?ids= pre-ticks the sensor (the detail page link)', () => {
    resetStore(storeState({ sensors: calibSensors() }))
    at(`/calibrate?ids=${deviceId(2)}`)
    render(<CalibrateScreen />)
    expect(screen.getByRole('checkbox', { name: /센서 2/ })).toBeChecked()
    expect(screen.getByRole('button', { name: '다음: 사전점검 (1대)' })).toBeEnabled()
  })

  it('shows 409 batch_active in words', async () => {
    mockApi({
      'POST /api/preflight': { status: 200, body: { checked_at: NOW_S, results: [presence(1, false)] } },
      'POST /api/batches': { status: 409, body: { error: { code: 'batch_active', message: 'busy' } } },
    })
    resetStore(storeState({ sensors: calibSensors() }))
    at(`/calibrate?ids=${deviceId(1)}`)
    render(<CalibrateScreen />)
    fireEvent.click(screen.getByRole('button', { name: '다음: 사전점검 (1대)' }))
    await screen.findByText('비어 있음')
    fireEvent.click(screen.getByRole('button', { name: '보정 시작' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('이미 진행 중인 보정이 있습니다')
  })
})

describe('CalibrateScreen: a batch from the server', () => {
  it('running: progress for every screen, with the "not device progress" note', () => {
    const running = batchView({
      state: 'running',
      jobs: [
        job(1, { state: 'learning', elapsed_s: 72.9, started: true }),
        job(2, { state: 'starting' }),
        succeededJob(3),
      ],
    })
    resetStore(storeState({ sensors: calibSensors(), batch: running }))
    render(<CalibrateScreen />)
    expect(screen.getByRole('heading', { name: '보정 중 · 1/3 끝남' })).toBeInTheDocument()
    expect(
      screen.getByText('막대는 예상 시간(약 3:00) 기준입니다. 센서는 진행률을 알려 주지 않습니다.'),
    ).toBeInTheDocument()
    expect(screen.getByText('보정하는 동안 센서 추가를 멈췄습니다')).toBeInTheDocument()
    const bar = screen.getByRole('progressbar')
    expect(bar).toHaveAttribute('aria-valuenow', '41')
    expect(bar).toHaveAttribute('aria-valuetext', '약 41% (예상 시간 기준)')
    expect(screen.getByText('1:12 / 약 3:00')).toBeInTheDocument()
    expect(screen.getByText('진행').closest('li')).toHaveAttribute('aria-current', 'step')
  })

  it('running cancel asks first, then cancels', async () => {
    const calls = mockApi({ [`POST /api/batches/${BATCH_ID}/cancel`]: { status: 200, body: batchView() } })
    resetStore(storeState({ sensors: calibSensors(), batch: batchView({ state: 'running' }) }))
    render(<CalibrateScreen />)
    fireEvent.click(screen.getByRole('button', { name: '보정 취소' }))
    expect(calls).toHaveLength(0)
    const dialog = screen.getByRole('dialog')
    expect(within(dialog).getByText(/센서 연결을 끊어 학습을 멈춥니다/)).toBeInTheDocument()
    fireEvent.click(within(dialog).getByRole('button', { name: '보정 취소' }))
    await waitFor(() =>
      expect(calls).toEqual([{ path: `/api/batches/${BATCH_ID}/cancel`, method: 'POST', body: undefined }]),
    )
  })

  it('waiting: a countdown and a one-tap cancel', async () => {
    const calls = mockApi({ [`POST /api/batches/${BATCH_ID}/cancel`]: { status: 200, body: batchView() } })
    const waiting = batchView({
      state: 'waiting',
      start: 'delay',
      presence_override: true,
      jobs: [job(1), job(2), job(3)],
    })
    act(() => {
      useStore
        .getState()
        .applyMessage(snapshotMsg({ sensors: calibSensors(), batch: { ...waiting, fire_at: NOW_S + 42 } }))
    })
    render(<CalibrateScreen />)
    expect(screen.getByRole('timer')).toHaveAccessibleName('0:42 후 보정 시작')
    expect(screen.getByText('재실 경고를 무시하고 시작함')).toBeInTheDocument()
    expect(screen.getAllByText('시작 대기')).toHaveLength(3)
    fireEvent.click(screen.getByRole('button', { name: '카운트다운 취소' }))
    await waitFor(() => expect(calls).toHaveLength(1))
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('waiting with under 5 s left: the cancel asks first (G38)', async () => {
    const calls = mockApi({ [`POST /api/batches/${BATCH_ID}/cancel`]: { status: 200, body: batchView() } })
    const waiting = batchView({ state: 'waiting', start: 'delay', jobs: [job(1), job(2), job(3)] })
    act(() => {
      useStore
        .getState()
        .applyMessage(snapshotMsg({ sensors: calibSensors(), batch: { ...waiting, fire_at: NOW_S + 3 } }))
    })
    render(<CalibrateScreen />)
    fireEvent.click(screen.getByRole('button', { name: '카운트다운 취소' }))
    expect(calls).toHaveLength(0)
    const dialog = screen.getByRole('dialog')
    expect(within(dialog).getByText(/곧 보정이 시작됩니다/)).toBeInTheDocument()
    fireEvent.click(within(dialog).getByRole('button', { name: '카운트다운 취소' }))
    await waitFor(() => expect(calls).toHaveLength(1))
  })

  it('the open confirm follows the round: running changes the words, an end closes it (G38)', () => {
    mockApi({})
    const waiting = batchView({ state: 'waiting', start: 'delay', jobs: [job(1), job(2), job(3)] })
    act(() => {
      useStore
        .getState()
        .applyMessage(snapshotMsg({ sensors: calibSensors(), batch: { ...waiting, fire_at: NOW_S + 2 } }))
    })
    render(<CalibrateScreen />)
    fireEvent.click(screen.getByRole('button', { name: '카운트다운 취소' }))
    act(() => useStore.setState({ batch: { ...waiting, state: 'running' }, countdown: null }))
    expect(within(screen.getByRole('dialog')).getByText(/보정을 취소하면 센서 연결을 끊어/)).toBeInTheDocument()
    act(() => useStore.setState({ batch: { ...waiting, state: 'cancelled' } }))
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('done: results, before/after, and a retry for the reconnected sensor only', async () => {
    const calls = mockApi({ [`POST /api/batches/${BATCH_ID}/retry`]: { status: 202, body: batchView() } })
    resetStore(
      storeState({
        sensors: calibSensors((n) => (n === 2 ? { live: live(2, { link: 'lost' }) } : {})),
        batch: batchView(),
      }),
    )
    render(<CalibrateScreen />)
    expect(screen.getByText('보정 끝 · 성공 2 · 실패 1')).toBeInTheDocument()
    expect(screen.getAllByText('보정 기록에 저장했습니다')).toHaveLength(2)
    expect(screen.getAllByText('70 → 64 (−6)')).toHaveLength(2)
    expect(screen.getAllByText('62 → 66 (+4)')).toHaveLength(2)
    expect(screen.getAllByText('센서 추가를 켜고 버튼을 다시 누르세요').length).toBeGreaterThan(0)
    expect(screen.getByRole('button', { name: '센서 추가 시작' })).toBeInTheDocument()
    expect(screen.getByRole('checkbox', { name: '센서 2' })).toBeDisabled()
    expect(screen.getByRole('button', { name: '다시 시도 (0대)' })).toBeDisabled()

    // the operator presses the button: sensor 2 is back
    act(() => {
      useStore.setState((s) => ({
        sensors: { ...s.sensors, [deviceId(2)]: { ...s.sensors[deviceId(2)]!, live: live(2) } },
      }))
    })
    expect(screen.getByRole('checkbox', { name: '센서 2' })).toBeChecked()
    expect(screen.getByRole('radio', { name: 'N초 후' })).toBeChecked() // time to leave the room
    fireEvent.click(screen.getByRole('button', { name: '다시 시도 (1대)' }))
    await waitFor(() => expect(calls).toHaveLength(1))
    expect(calls[0]?.body).toEqual({ device_ids: [deviceId(2)], start: 'delay', delay_s: 30 })
  })

  it('"새 보정" dismisses the results on this screen only', () => {
    resetStore(
      storeState({
        sensors: calibSensors(),
        batch: batchView({ jobs: [succeededJob(1), succeededJob(2), succeededJob(3)] }),
      }),
    )
    render(<CalibrateScreen />)
    fireEvent.click(screen.getByRole('button', { name: '새 보정' }))
    expect(screen.getByRole('heading', { name: '보정할 센서를 고르세요' })).toBeInTheDocument()
    expect(sessionStorage.getItem('ms605.dismissedBatch')).toBe(BATCH_ID)
    expect(useStore.getState().batch).not.toBeNull()
  })
})

describe('CalibrationPill', () => {
  it('waiting, running and nothing', () => {
    resetStore(storeState({ batch: batchView({ state: 'waiting', start: 'delay' }) }))
    act(() => {
      useStore.setState({ countdown: { batch_id: BATCH_ID, remaining_s: 42, received_at: Date.now() } })
    })
    const { rerender } = render(<CalibrationPill />)
    expect(screen.getByRole('link')).toHaveTextContent('보정 0:42 후')
    expect(screen.getByRole('link')).toHaveAttribute('href', '/calibrate')

    act(() => {
      useStore.setState({
        batch: batchView({ state: 'running', jobs: [succeededJob(1), job(2), job(3)] }),
        countdown: null,
      })
    })
    rerender(<CalibrationPill />)
    expect(screen.getByRole('link')).toHaveTextContent('보정 중 1/3')

    act(() => {
      useStore.setState({ batch: batchView() })
    })
    rerender(<CalibrationPill />)
    expect(screen.queryByRole('link')).not.toBeInTheDocument()
  })
})

describe('operation lock (G22)', () => {
  it('a sensor in the running round cannot be released from its detail page', () => {
    resetStore(storeState({ sensors: calibSensors(), batch: batchView({ state: 'running' }) }))
    render(<SensorDetailScreen deviceId={deviceId(1)} />)
    expect(screen.getByRole('button', { name: '연결 해제' })).toBeDisabled()
    expect(screen.getByText('보정이 끝난 뒤 해제할 수 있습니다')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: '이 센서 보정' })).toHaveAttribute('href', `/calibrate?ids=${deviceId(1)}`)
    expect(screen.getByRole('navigation', { name: '센서 메뉴' })).toBeInTheDocument()
  })
})
