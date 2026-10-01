import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ApplyPill } from '../components/ApplyPill'
import { AdvancedTab } from '../components/edit/AdvancedTab'
import { ApplyResults } from '../components/edit/ApplyResults'
import { DiffPreview } from '../components/edit/DiffPreview'
import { HistoryTab } from '../components/edit/HistoryTab'
import { RollbackPicker } from '../components/edit/RollbackPicker'
import { SettingsTab } from '../components/edit/SettingsTab'
import { setThreshold } from '../draft'
import { useDrafts } from '../store/drafts'
import { resetStore, useStore } from '../store/store'
import { apiError, mockApi } from './api'
import {
  applyItem,
  applyJob,
  batchView,
  calibrationHistoryOf,
  calibSensors,
  change,
  configView,
  deviceId,
  jobPartial,
  jobRunning,
  live,
  liveData,
  NOW_S,
  previewPlain,
  previewRisky,
  profile,
  SNAP_1,
  sensorPreview,
  snapshotList,
  storeState,
} from './fixtures'

const ID = deviceId(1)
const N = null
const RECT = { left: 0, width: 200, top: 0, right: 200, bottom: 32, height: 32, x: 0, y: 0, toJSON: () => ({}) }

beforeEach(() => {
  vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockReturnValue(RECT as DOMRect)
})

function connected(patch: Parameters<typeof storeState>[0] = {}) {
  resetStore({ ...storeState({ sensors: calibSensors(), ...patch }), live: { [ID]: liveData(1) } })
}

const drag = (slider: HTMLElement, ...xs: number[]) => {
  const [first, ...rest] = xs
  fireEvent.pointerDown(slider, { clientX: first, button: 0, pointerId: 1 })
  for (const x of rest) fireEvent.pointerMove(slider, { clientX: x, pointerId: 1 })
  fireEvent.pointerUp(slider, { pointerId: 1 })
}

describe('SettingsTab', () => {
  it('a drag makes the expected draft; preview and apply send exactly it', async () => {
    const calls = mockApi({
      [`GET /api/sensors/${ID}/config`]: { status: 200, body: configView(1) },
      'POST /api/drafts/preview': { status: 200, body: previewPlain() },
      'POST /api/apply': { status: 202, body: applyJob({ state: 'running', items: [applyItem(1, { state: 'queued' })] }) },
    })
    connected()
    render(<SettingsTab deviceId={ID} />)
    const slider = await screen.findByRole('slider', { name: 'Z0 재실 트리거' })
    expect(screen.queryByText(/^변경 \d+개$/)).not.toBeInTheDocument()

    drag(slider, 130) // axis 100 (live 64 × 1.25 = 80), 130/200 -> 65
    expect(useDrafts.getState().sensors[ID]?.edit.trigger).toEqual([65, N, N, N, N, N, N])
    expect(screen.getByText('변경 1개')).toBeInTheDocument()
    expect(slider).toHaveAttribute('aria-valuetext', '새 임계값 65, 현재 60 — 지금 값이 넘지 않음')

    fireEvent.click(screen.getByRole('button', { name: '미리보기' }))
    const dialog = await screen.findByRole('dialog')
    expect(calls.find((c) => c.path === '/api/drafts/preview')?.body).toEqual({
      targets: [ID],
      changes: {
        sensitivity: N,
        zone_enable: N,
        zone_thresholds: { mode: 'absolute', trigger: [65, N, N, N, N, N, N], maintain: [N, N, N, N, N, N, N] },
        subsensor_zones: N,
        subsensor_timing: N,
        subsensor_enable: N,
        dnd: N,
      },
      expect_rev: N,
    })
    const row = within(dialog).getByRole('row', { name: /Z0 재실 트리거/ })
    expect(row).toHaveTextContent('60 → 65 (+5)')

    fireEvent.click(within(dialog).getByRole('button', { name: '적용' }))
    await waitFor(() => expect(calls.some((c) => c.path === '/api/apply')).toBe(true))
    expect(calls.find((c) => c.path === '/api/apply')?.body).toMatchObject({ targets: [ID], expect_rev: { [ID]: 0 } })
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(useDrafts.getState().mine[`sensor:${ID}`]?.applyId).toBeDefined()
  })

  it('a rebase that keeps every edit still drops the shown diff: its before values may be old', async () => {
    const calls = mockApi({ 'POST /api/drafts/preview': { status: 200, body: previewPlain() } })
    connected()
    useDrafts.getState().openSensor(configView(1))
    useDrafts.getState().updateSensor(ID, (d) => setThreshold(d, 0, 'trigger', 65))
    render(<SettingsTab deviceId={ID} />)
    fireEvent.click(screen.getByRole('button', { name: '미리보기' }))
    await screen.findByRole('dialog')
    act(() => useDrafts.getState().rebase(configView(1, { read_at: NOW_S + 20 })))
    expect(useDrafts.getState().sensors[ID]?.edit.trigger[0]).toBe(65)
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '미리보기' }))
    await screen.findByRole('dialog')
    expect(calls.filter((c) => c.path === '/api/drafts/preview')).toHaveLength(2)
  })

  it('dragging back to the device value removes the change and the bar', async () => {
    mockApi({ [`GET /api/sensors/${ID}/config`]: { status: 200, body: configView(1) } })
    connected()
    render(<SettingsTab deviceId={ID} />)
    const slider = await screen.findByRole('slider', { name: 'Z0 재실 트리거' })
    drag(slider, 130)
    expect(screen.getByText('변경 1개')).toBeInTheDocument()
    drag(slider, 120)
    expect(screen.queryByText('변경 1개')).not.toBeInTheDocument()
    expect(useDrafts.getState().sensors[ID]?.edit.trigger).toEqual([N, N, N, N, N, N, N])
  })

  it('the live fill turns over the moment the new threshold goes under it', async () => {
    mockApi({ [`GET /api/sensors/${ID}/config`]: { status: 200, body: configView(1) } })
    connected()
    render(<SettingsTab deviceId={ID} />)
    const slider = await screen.findByRole('slider', { name: 'Z0 재실 트리거' })
    const meter = slider.closest('[data-over]')
    expect(meter).toHaveAttribute('data-over', 'true') // live 64 > 60
    fireEvent.keyDown(slider, { key: 'ArrowRight', shiftKey: true }) // 70
    expect(meter).toHaveAttribute('data-over', 'false')
    expect(slider).toHaveAttribute('aria-valuetext', '새 임계값 70, 현재 60 — 지금 값이 넘지 않음')
  })

  it('a 409 on preview is shown in words', async () => {
    mockApi({
      [`GET /api/sensors/${ID}/config`]: { status: 200, body: configView(1) },
      'POST /api/drafts/preview': apiError(409, 'apply_active'),
    })
    connected()
    render(<SettingsTab deviceId={ID} />)
    fireEvent.click(await screen.findByRole('radio', { name: '높음' }))
    expect(screen.getByText('변경 1개')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '미리보기' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('다른 설정 적용이 진행 중입니다')
  })

  it('needs a connection', () => {
    mockApi({})
    resetStore(storeState({ sensors: calibSensors((n) => (n === 1 ? { live: live(1, { link: 'lost' }) } : {})) }))
    render(<SettingsTab deviceId={ID} />)
    expect(screen.getByText('연결된 센서만 설정을 읽고 바꿀 수 있습니다')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: '이력' })).toHaveAttribute('href', `/sensors/${ID}/history`)
  })

  it('a sensor in the running calibration keeps its draft but cannot edit it', () => {
    mockApi({})
    connected({ batch: batchView({ state: 'running' }) })
    useDrafts.getState().openSensor(configView(1))
    render(<SettingsTab deviceId={ID} />)
    expect(screen.getByText('보정이 끝난 뒤 편집할 수 있습니다')).toBeInTheDocument()
    expect(screen.getByRole('slider', { name: 'Z0 재실 트리거' })).toHaveAttribute('aria-disabled', 'true')
    expect(screen.getByRole('radio', { name: '보통' })).toBeDisabled()
  })

  it('a sensor in a running apply shows the job and locks the editor', () => {
    mockApi({})
    connected({ apply: jobRunning() })
    useDrafts.getState().openSensor(configView(1))
    render(<SettingsTab deviceId={ID} />)
    expect(screen.getByText('설정을 적용하는 중입니다. 끝난 뒤 편집할 수 있습니다')).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: '설정 적용 중 · 1/1' })).toBeInTheDocument()
  })

  it('config_rev moved: a banner offers the new values, which rebase the draft', async () => {
    mockApi({ [`GET /api/sensors/${ID}/config`]: { status: 200, body: configView(1, { config_rev: 1 }) } })
    connected({ sensors: calibSensors((n) => (n === 1 ? { config_rev: 1 } : {})) })
    useDrafts.getState().openSensor(configView(1))
    useDrafts.getState().updateSensor(ID, (d) => setThreshold(d, 1, 'trigger', 70))
    render(<SettingsTab deviceId={ID} />)
    expect(screen.getByText('다른 화면이나 보정이 이 센서의 설정을 바꿨습니다')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '새 값 불러오기' }))
    await waitFor(() => expect(screen.queryByText('다른 화면이나 보정이 이 센서의 설정을 바꿨습니다')).not.toBeInTheDocument())
    expect(useDrafts.getState().sensors[ID]?.edit.trigger[1]).toBe(70)
  })

  it('the sensor came back after the draft was read: the base is read again, the edits stay', async () => {
    const fresh = profile()
    fresh.zone_thresholds[0] = { trigger: 80, maintain: 30 } // changed while it was away (vendor app)
    const calls = mockApi({
      [`GET /api/sensors/${ID}/config`]: { status: 200, body: configView(1, { read_at: NOW_S + 20, profile: fresh }) },
    })
    connected()
    useDrafts.getState().openSensor(configView(1))
    useDrafts.getState().updateSensor(ID, (d) => setThreshold(d, 1, 'trigger', 70))
    render(<SettingsTab deviceId={ID} />)
    await screen.findByRole('slider', { name: 'Z0 재실 트리거' })
    expect(calls).toHaveLength(0) // gathered before the read: nothing to refresh
    const back = calibSensors((n) => (n === 1 ? { live: live(1, { gathered_at: NOW_S + 10 }) } : {}))
    act(() => useStore.setState({ sensors: Object.fromEntries(back.map((s) => [s.device_id, s])) }))
    await waitFor(() => expect(useDrafts.getState().sensors[ID]?.base.read_at).toBe(NOW_S + 20))
    expect(useDrafts.getState().sensors[ID]?.edit.trigger[1]).toBe(70)
    expect(screen.getByRole('slider', { name: 'Z0 재실 트리거' })).toHaveAttribute('aria-valuenow', '80')
    expect(calls.filter((c) => c.path === `/api/sensors/${ID}/config`)).toHaveLength(1)
  })

  it('모두 되돌리기 clears the edits and reads the device again', async () => {
    const calls = mockApi({ [`GET /api/sensors/${ID}/config`]: { status: 200, body: configView(1, { read_at: NOW_S + 5 }) } })
    connected()
    useDrafts.getState().openSensor(configView(1))
    useDrafts.getState().updateSensor(ID, (d) => setThreshold(d, 1, 'trigger', 70))
    render(<SettingsTab deviceId={ID} />)
    fireEvent.click(screen.getByRole('button', { name: '모두 되돌리기' }))
    await waitFor(() => expect(useDrafts.getState().sensors[ID]?.base.read_at).toBe(NOW_S + 5))
    expect(useDrafts.getState().sensors[ID]?.edit.trigger).toEqual([N, N, N, N, N, N, N])
    expect(calls.filter((c) => c.path === `/api/sensors/${ID}/config`)).toHaveLength(1)
  })

  it('my apply verified: the draft clears and the device is read again', async () => {
    const calls = mockApi({ [`GET /api/sensors/${ID}/config`]: { status: 200, body: configView(1, { config_rev: 1 }) } })
    connected()
    useDrafts.getState().openSensor(configView(1))
    useDrafts.getState().updateSensor(ID, (d) => setThreshold(d, 0, 'trigger', 65))
    useDrafts.getState().setMine(`sensor:${ID}`, { applyId: 'a1', settled: false })
    render(<SettingsTab deviceId={ID} />)
    act(() => useStore.setState({ apply: applyJob({ apply_id: 'a1', items: [applyItem(1)] }) }))
    await waitFor(() => expect(useDrafts.getState().sensors[ID]?.base.config_rev).toBe(1))
    expect(useDrafts.getState().sensors[ID]?.edit.trigger).toEqual([N, N, N, N, N, N, N])
    expect(calls.filter((c) => c.path === `/api/sensors/${ID}/config`)).toHaveLength(1)
    expect(screen.getByRole('heading', { name: '설정 적용 끝 · 모두 확인함 (1대)' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '닫기' }))
    expect(screen.queryByRole('heading', { name: /설정 적용 끝/ })).not.toBeInTheDocument()
  })
})

describe('AdvancedTab', () => {
  it('zone chips edit the sorted draft; no zone on an enabled sub-sensor warns', () => {
    mockApi({})
    connected()
    useDrafts.getState().openSensor(configView(1))
    render(<AdvancedTab deviceId={ID} />)
    const s1 = screen.getByRole('group', { name: 'S1 구역' })
    fireEvent.click(within(s1).getByRole('button', { name: 'Z1' }))
    expect(within(s1).getByRole('button', { name: 'Z1' })).toHaveAttribute('aria-pressed', 'false')
    expect(useDrafts.getState().sensors[ID]?.edit.subsensor_zones).toEqual([[0, 2], [3, 4], [5, 6]])
    fireEvent.click(within(s1).getByRole('button', { name: 'Z4' }))
    expect(useDrafts.getState().sensors[ID]?.edit.subsensor_zones).toEqual([[0, 2, 4], [3, 4], [5, 6]])
    for (const z of ['Z0', 'Z2', 'Z4']) fireEvent.click(within(s1).getByRole('button', { name: z }))
    expect(screen.getByText('구역이 없으면 이 서브센서는 감지하지 않습니다')).toBeInTheDocument()
    expect(screen.getByText('변경 1개')).toBeInTheDocument()
  })

  it('timing and DND are in the same draft', () => {
    mockApi({})
    connected()
    useDrafts.getState().openSensor(configView(1))
    render(<AdvancedTab deviceId={ID} />)
    fireEvent.change(screen.getAllByRole('spinbutton', { name: '재실 유지 시간' })[0]!, { target: { value: '10' } })
    fireEvent.click(screen.getByRole('switch', { name: '방해 금지 (DND)' }))
    const e = useDrafts.getState().sensors[ID]?.edit
    expect(e?.subsensor_timing).toEqual([[10, 30], [5, 30], [5, 30]])
    expect(e?.dnd).toBe(true)
    expect(screen.getByText('변경 2개')).toBeInTheDocument()
  })

  it('an unreadable DND is said, not offered', () => {
    mockApi({})
    connected()
    useDrafts.getState().openSensor(configView(1, { profile: profile({ dnd: null }) }))
    render(<AdvancedTab deviceId={ID} />)
    expect(screen.getByText('이 센서에서 방해 금지 상태를 읽지 못했습니다')).toBeInTheDocument()
    expect(screen.queryByRole('switch', { name: '방해 금지 (DND)' })).not.toBeInTheDocument()
  })

  it('time sync is an action: it posts and shows the result', async () => {
    const calls = mockApi({
      'POST /api/time-sync': { status: 200, body: { items: [{ device_id: ID, written_at: NOW_S, error: null }] } },
    })
    connected()
    useDrafts.getState().openSensor(configView(1))
    render(<AdvancedTab deviceId={ID} />)
    fireEvent.click(screen.getByRole('button', { name: '센서 시계 맞추기' }))
    expect(await screen.findByText(/에 맞췄습니다$/)).toBeInTheDocument()
    expect(calls).toEqual([{ path: '/api/time-sync', method: 'POST', body: { device_ids: [ID] } }])
    expect(useDrafts.getState().sensors[ID]?.edit.dnd).toBeNull()
  })
})

describe('HistoryTab', () => {
  it('calibration records, backups, and the experimental device records', async () => {
    const calls = mockApi({
      [`GET /api/sensors/${ID}/history`]: { status: 200, body: calibrationHistoryOf(1) },
      [`GET /api/sensors/${ID}/snapshots`]: { status: 200, body: snapshotList(1) },
      [`GET /api/sensors/${ID}/device-history?kind=presence&detail=false`]: {
        status: 200,
        body: { device_id: ID, kind: 'presence', detail: false, read_at: NOW_S, presence: [], light: [] },
      },
    })
    connected()
    render(<HistoryTab deviceId={ID} />)
    expect(await screen.findByText('사용자')).toBeInTheDocument()
    expect(screen.getByText('보통')).toBeInTheDocument()
    expect(await screen.findByText('가장 최근')).toBeInTheDocument()
    expect(screen.getByText('실험적')).toBeInTheDocument()
    fireEvent.click(screen.getByText('실험적').closest('summary')!)
    fireEvent.click(screen.getByRole('button', { name: '기기에서 읽기' }))
    expect(await screen.findByText('센서에 기록이 없습니다')).toBeInTheDocument()
    expect(calls.map((c) => c.path)).toContain(`/api/sensors/${ID}/device-history?kind=presence&detail=false`)
  })

  it('no records: says so', async () => {
    mockApi({
      [`GET /api/sensors/${ID}/history`]: { status: 200, body: { device_id: ID, records: [] } },
      [`GET /api/sensors/${ID}/snapshots`]: { status: 200, body: { device_id: ID, snapshots: [] } },
    })
    connected()
    render(<HistoryTab deviceId={ID} />)
    expect(await screen.findByText('보정 기록이 없습니다')).toBeInTheDocument()
    expect(await screen.findByText(/설정 백업이 없습니다/)).toBeInTheDocument()
  })
})

describe('RollbackPicker', () => {
  it('pick a point, preview the way back, roll back', async () => {
    const calls = mockApi({
      [`GET /api/sensors/${ID}/snapshots`]: { status: 200, body: snapshotList(1) },
      [`GET /api/sensors/${ID}/snapshots/${SNAP_1}`]: {
        status: 200,
        body: { device_id: ID, snapshot: snapshotList(1).snapshots[0], profile: profile() },
      },
      'POST /api/rollback/preview': { status: 200, body: { ...previewPlain(), kind: 'rollback' } },
      'POST /api/rollback': { status: 202, body: applyJob({ kind: 'rollback', state: 'running' }) },
    })
    connected()
    const onStarted = vi.fn()
    render(<RollbackPicker deviceId={ID} connected locked={false} onStarted={onStarted} />)
    const radios = await screen.findAllByRole('radio')
    expect(radios).toHaveLength(2)
    expect(within(radios[0]!.closest('li')!).getByText('가장 최근')).toBeInTheDocument()
    expect(screen.getByText('설정 적용 전')).toBeInTheDocument()
    expect(screen.getByText('되돌리기 전')).toBeInTheDocument()
    const go = screen.getByRole('button', { name: '이 시점으로 되돌리기 미리보기' })
    expect(go).toBeDisabled()
    fireEvent.click(radios[0]!)
    expect(await screen.findByText(/민감도 보통/)).toBeInTheDocument()
    fireEvent.click(go)
    fireEvent.click(await screen.findByRole('button', { name: '되돌리기' }))
    await waitFor(() => expect(onStarted).toHaveBeenCalled())
    expect(calls.find((c) => c.path === '/api/rollback/preview')?.body).toEqual({
      items: [{ device_id: ID, snapshot: SNAP_1 }],
      expect_rev: null,
    })
    expect(calls.find((c) => c.path === '/api/rollback')?.body).toEqual({
      items: [{ device_id: ID, snapshot: SNAP_1 }],
      expect_rev: { [ID]: 0 },
    })
  })

  it('without a connection the list shows but the preview is off', async () => {
    mockApi({ [`GET /api/sensors/${ID}/snapshots`]: { status: 200, body: snapshotList(1) } })
    render(<RollbackPicker deviceId={ID} connected={false} locked={false} onStarted={() => {}} />)
    fireEvent.click((await screen.findAllByRole('radio'))[1]!)
    expect(screen.getByText('연결된 센서만 설정을 읽고 바꿀 수 있습니다')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '이 시점으로 되돌리기 미리보기' })).toBeDisabled()
  })
})

describe('DiffPreview', () => {
  const names = { [deviceId(1)]: '센서 1', [deviceId(2)]: '센서 2', [deviceId(3)]: '센서 3' }

  it('risky rows are marked and worded; an unreadable sensor blocks apply and can be dropped', () => {
    const onDrop = vi.fn()
    render(
      <DiffPreview preview={previewRisky()} names={names} onApply={vi.fn()} onBack={vi.fn()} applyLabel="적용 (3대)" onDropErrors={onDrop} />,
    )
    expect(screen.getByText('3대 · 4칸 바뀜 · 주의 4칸')).toBeInTheDocument()
    const risky = document.querySelectorAll('tr[data-risk]')
    expect(risky).toHaveLength(4)
    expect(within(risky[0] as HTMLElement).getByText('크게 바뀝니다 (20 이상)')).toBeInTheDocument()
    expect(screen.getByText('이 센서는 적용할 수 없습니다: busy: read')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '적용 (3대)' })).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: '그 센서를 빼고 다시 미리보기' }))
    expect(onDrop).toHaveBeenCalledWith([deviceId(3)])
  })

  it('absolute overwrite needs the tick; it carries over from the bulk screen', () => {
    const p = previewRisky()
    p.items = p.items.slice(0, 2)
    const { unmount } = render(<DiffPreview preview={p} names={names} onApply={vi.fn()} onBack={vi.fn()} applyLabel="적용" />)
    const apply = screen.getByRole('button', { name: '적용' })
    expect(apply).toBeDisabled()
    fireEvent.click(screen.getByRole('checkbox', { name: '위 센서들의 보정값을 덮어쓰는 것을 확인했습니다' }))
    expect(apply).toBeEnabled()
    unmount()
    render(<DiffPreview preview={p} names={names} onApply={vi.fn()} onBack={vi.fn()} applyLabel="적용" ackInitially />)
    expect(screen.getByRole('checkbox', { name: '위 센서들의 보정값을 덮어쓰는 것을 확인했습니다' })).toBeChecked()
    expect(screen.getByRole('button', { name: '적용' })).toBeEnabled()
  })

  it('nothing to change: says so and stays off', () => {
    const p = { ...previewPlain(), items: [sensorPreview(1, { changes: [] })] }
    render(<DiffPreview preview={p} names={names} onApply={vi.fn()} onBack={vi.fn()} applyLabel="적용" />)
    expect(screen.getAllByText('바뀌는 것이 없습니다').length).toBeGreaterThan(0)
    expect(screen.getByRole('button', { name: '적용' })).toBeDisabled()
  })

  it('an apply error (409 stale) stays in the preview', async () => {
    const onApply = vi.fn(async () => {
      const { ApiRequestError } = await import('../api/client')
      throw new ApiRequestError(409, 'stale', 'moved')
    })
    render(
      <DiffPreview
        preview={{ ...previewPlain(), items: [sensorPreview(1, { changes: [change({ section: 'dnd', index: N, part: 'value', before: false, after: true, risks: ['dnd_on'] })] })] }}
        names={names}
        onApply={onApply}
        onBack={vi.fn()}
        applyLabel="적용"
      />,
    )
    fireEvent.click(screen.getByRole('button', { name: '적용' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('미리보기 뒤에 설정이 바뀌었습니다. 미리보기를 다시 하세요')
  })
})

describe('ApplyResults', () => {
  it('partial failure at a glance, rollback only where a snapshot exists', () => {
    resetStore(storeState({ sensors: calibSensors() }))
    const job = jobPartial()
    job.items.push(applyItem(4, { state: 'failed', snapshot: null, error: 'busy: read' }))
    render(<ApplyResults job={job} onDismiss={vi.fn()} dismissLabel="새 초안" />)
    expect(screen.getByRole('heading', { name: '설정 적용 끝 · 확인함 1 · 일부 1 · 실패 2' })).toBeInTheDocument()
    expect(screen.getByText('적용됨 · 확인함')).toBeInTheDocument()
    expect(screen.getByText('실패 (쓰는 중)')).toBeInTheDocument()
    expect(screen.getByText('일부만 반영됨')).toBeInTheDocument()
    expect(screen.getByText('시작하지 못함 (다른 작업 중)')).toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: /^센서 \d 되돌리기$/ })).toHaveLength(3)
    expect(screen.getByRole('button', { name: '모두 되돌리기' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '새 초안' })).toBeInTheDocument()
  })

  it('the contract fixture: verified · failed (snapshot) · partial', () => {
    resetStore(storeState({ sensors: calibSensors() }))
    render(<ApplyResults job={jobPartial()} onDismiss={vi.fn()} />)
    expect(screen.getByRole('heading', { name: '설정 적용 끝 · 확인함 1 · 일부 1 · 실패 1' })).toBeInTheDocument()
  })

  it('a row rollback opens the way-back preview for that sensor', async () => {
    const calls = mockApi({ 'POST /api/rollback/preview': { status: 200, body: { ...previewPlain(), kind: 'rollback' } } })
    resetStore(storeState({ sensors: calibSensors() }))
    render(<ApplyResults job={jobPartial()} onDismiss={vi.fn()} />)
    fireEvent.click(screen.getByRole('button', { name: '센서 2 되돌리기' }))
    expect(await screen.findByRole('dialog')).toBeInTheDocument()
    await waitFor(() =>
      expect(calls[0]?.body).toEqual({ items: [{ device_id: deviceId(2), snapshot: SNAP_1 }], expect_rev: null }),
    )
  })

  it('while running: no rollback, no dismiss', () => {
    resetStore(storeState({ sensors: calibSensors() }))
    render(<ApplyResults job={jobRunning()} onDismiss={vi.fn()} />)
    expect(screen.getByRole('heading', { name: '설정 적용 중 · 1/3' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /되돌리기/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '닫기' })).not.toBeInTheDocument()
  })
})

describe('ApplyPill', () => {
  it('running only; one target links to its settings, several to /bulk', () => {
    resetStore(storeState({ apply: jobRunning() }))
    const { rerender } = render(<ApplyPill />)
    expect(screen.getByRole('link', { name: '설정 적용 1/3' })).toHaveAttribute('href', '/bulk')
    act(() => useStore.setState({ apply: applyJob({ state: 'running', items: [applyItem(2, { state: 'queued' })] }) }))
    rerender(<ApplyPill />)
    expect(screen.getByRole('link', { name: '설정 적용 0/1' })).toHaveAttribute('href', `/sensors/${deviceId(2)}/settings`)
    act(() => useStore.setState({ apply: applyJob() }))
    rerender(<ApplyPill />)
    expect(screen.queryByRole('link')).not.toBeInTheDocument()
  })
})
