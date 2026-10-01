import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { DraftIn } from '../api/types'
import { BulkEditScreen } from '../screens/BulkEdit'
import { useDrafts } from '../store/drafts'
import { resetStore } from '../store/store'
import { apiError, mockApi } from './api'
import {
  applyJob,
  batchView,
  calibSensors,
  configView,
  deviceId,
  previewPlain,
  previewRisky,
  storeState,
} from './fixtures'

const ids = [1, 2, 3].map(deviceId)
const N = null
const at = (path: string) => window.history.replaceState(null, '', path)
afterEach(() => {
  at('/')
  sessionStorage.clear()
})

const absolutePreview = () => {
  const p = previewRisky()
  p.items = p.items.slice(0, 2)
  return p
}

function open(path = '/bulk', patch: Parameters<typeof storeState>[0] = {}) {
  at(path)
  resetStore(storeState({ sensors: calibSensors(), ...patch }))
  render(<BulkEditScreen />)
}

describe('BulkEditScreen: edit', () => {
  it('relative is the default and +5 goes out as relative', async () => {
    const calls = mockApi({ 'POST /api/drafts/preview': { status: 200, body: previewPlain() } })
    open()
    expect(screen.getByRole('radio', { name: '상대값 (기본)' })).toBeChecked()
    expect(screen.getByText(/각 센서의 지금 값에 더하거나 뺍니다/)).toBeInTheDocument()
    const go = screen.getByRole('button', { name: '미리보기 (0대)' })
    expect(go).toBeDisabled()
    expect(screen.getByText('대상 센서를 고르세요')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '연결된 센서 모두 선택' }))
    expect(screen.getByText('바꿀 값을 넣으세요')).toBeInTheDocument()
    fireEvent.change(screen.getByRole('textbox', { name: '모든 존 재실 트리거' }), { target: { value: '+5' } })
    expect(screen.getByRole('textbox', { name: 'Z3 재실 트리거' })).toHaveValue('+5')
    fireEvent.click(screen.getByRole('button', { name: 'Z2 재실 유지 줄이기' }))
    expect(screen.getByRole('textbox', { name: 'Z2 재실 유지' })).toHaveValue('−1')

    fireEvent.click(screen.getByRole('button', { name: '미리보기 (3대)' }))
    await waitFor(() => expect(calls).toHaveLength(1))
    const body = calls[0]?.body as DraftIn
    expect(body.targets).toEqual(ids)
    expect(body.changes.zone_thresholds).toEqual({
      mode: 'relative',
      trigger: [5, 5, 5, 5, 5, 5, 5],
      maintain: [N, N, -1, N, N, N, N],
    })
    expect(body.expect_rev).toBeNull()
  })

  it('absolute: values clear, the warning shows, and preview waits for the tick', () => {
    mockApi({})
    open(`/bulk?ids=${ids.join(',')}`)
    fireEvent.change(screen.getByRole('textbox', { name: 'Z0 재실 트리거' }), { target: { value: '+5' } })
    fireEvent.click(screen.getByRole('radio', { name: '절대값' }))
    expect(screen.getByRole('textbox', { name: 'Z0 재실 트리거' })).toHaveValue('')
    expect(screen.getByRole('alert')).toHaveTextContent('절대값은 각 센서의 보정 결과를 같은 값으로 덮어씁니다')
    // no per-sensor base to step from: − and + wait for a typed value instead of stepping from 0
    expect(screen.getByRole('button', { name: 'Z0 재실 트리거 늘리기' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Z0 재실 트리거 줄이기' })).toBeDisabled()
    fireEvent.change(screen.getByRole('textbox', { name: 'Z0 재실 트리거' }), { target: { value: '60' } })
    expect(screen.getByRole('button', { name: 'Z0 재실 트리거 늘리기' })).toBeEnabled()
    const go = screen.getByRole('button', { name: '미리보기 (3대)' })
    expect(go).toBeDisabled()
    expect(screen.getByText('덮어쓰기 확인을 체크하세요')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('checkbox', { name: '보정값을 덮어쓰는 것을 이해했습니다' }))
    expect(go).toBeEnabled()
  })

  it('the overwrite tick carries into the diff; after 202 the values go and the selection stays (G37)', async () => {
    const calls = mockApi({
      'POST /api/drafts/preview': { status: 200, body: absolutePreview() },
      'POST /api/apply': { status: 202, body: applyJob({ state: 'running' }) },
    })
    open(`/bulk?ids=${ids.slice(0, 2).join(',')}`)
    fireEvent.click(screen.getByRole('radio', { name: '절대값' }))
    fireEvent.change(screen.getByRole('textbox', { name: 'Z0 재실 트리거' }), { target: { value: '60' } })
    fireEvent.click(screen.getByRole('checkbox', { name: '보정값을 덮어쓰는 것을 이해했습니다' }))
    fireEvent.click(screen.getByRole('button', { name: '미리보기 (2대)' }))
    expect(await screen.findByRole('checkbox', { name: '위 센서들의 보정값을 덮어쓰는 것을 확인했습니다' })).toBeChecked()
    expect(screen.getAllByText('각 센서의 보정값을 같은 값으로 덮어씁니다').length).toBeGreaterThan(0)
    fireEvent.click(screen.getByRole('button', { name: '적용 (2대)' }))
    await waitFor(() => expect(calls.map((c) => c.path)).toContain('/api/apply'))
    expect((calls[1]?.body as DraftIn).expect_rev).toEqual({ [ids[0]!]: 0, [ids[1]!]: 0 })
    const b = useDrafts.getState().bulk
    expect(b?.trigger).toEqual([N, N, N, N, N, N, N])
    expect(b?.ids).toEqual(ids.slice(0, 2))
    expect(useDrafts.getState().mine.bulk?.applyId).toBeDefined()
  })

  it('409 apply_active is said in words', async () => {
    mockApi({
      'POST /api/drafts/preview': { status: 200, body: previewPlain() },
      'POST /api/apply': apiError(409, 'apply_active'),
    })
    open(`/bulk?ids=${ids[0]}`)
    fireEvent.change(screen.getByRole('textbox', { name: 'Z0 재실 트리거' }), { target: { value: '3' } })
    fireEvent.click(screen.getByRole('button', { name: '미리보기 (1대)' }))
    fireEvent.click(await screen.findByRole('button', { name: '적용 (1대)' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('다른 설정 적용이 진행 중입니다')
  })

  it('drop-errors waits for its re-preview, and a failed one is said on the diff', async () => {
    let answer: (r: Response) => void = () => {}
    const fetchMock = vi.fn(() =>
      fetchMock.mock.calls.length === 1
        ? Promise.resolve(new Response(JSON.stringify(previewRisky()), { status: 200 }))
        : new Promise<Response>((resolve) => {
            answer = resolve
          }),
    )
    vi.stubGlobal('fetch', fetchMock)
    open(`/bulk?ids=${ids.join(',')}`)
    fireEvent.change(screen.getByRole('textbox', { name: 'Z0 재실 트리거' }), { target: { value: '+5' } })
    fireEvent.click(screen.getByRole('button', { name: '미리보기 (3대)' }))
    const drop = await screen.findByRole('button', { name: '그 센서를 빼고 다시 미리보기' })
    fireEvent.click(drop)
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2))
    expect(drop).toBeDisabled()
    answer(new Response(JSON.stringify({ error: { code: 'apply_active', message: '' } }), { status: 409 }))
    const said = await screen.findByText('다른 설정 적용이 진행 중입니다', { exact: false })
    expect(said.closest('[role="alert"]')).not.toBeNull()
    expect(screen.getByRole('button', { name: '그 센서를 빼고 다시 미리보기' })).toBeEnabled()
  })

  it('a sensor in the running calibration cannot be picked', () => {
    mockApi({})
    open('/bulk', { batch: batchView({ state: 'running', round_ids: [ids[1]!] }) })
    expect(screen.getByRole('checkbox', { name: /센서 2/ })).toBeDisabled()
    expect(screen.getByText('보정 중인 센서는 고를 수 없습니다')).toBeInTheDocument()
  })

  it('a job of mine shows its results until 새 초안', () => {
    mockApi({})
    useDrafts.getState().setMine('bulk', { applyId: applyJob().apply_id, settled: false })
    open('/bulk', { apply: applyJob() })
    expect(screen.getByRole('heading', { name: '설정 적용 끝 · 모두 확인함 (3대)' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '새 초안' }))
    expect(screen.getByRole('radio', { name: '상대값 (기본)' })).toBeInTheDocument()
    expect(sessionStorage.getItem('ms605.dismissedApply')).toBe(applyJob().apply_id)
  })
})

describe('BulkEditScreen: clone', () => {
  it('source from the query, default sections, threshold warning, the preview body', async () => {
    const calls = mockApi({
      [`GET /api/sensors/${ids[0]}/config`]: { status: 200, body: configView(1, { config_rev: 3 }) },
      'POST /api/clone/preview': { status: 200, body: { ...absolutePreview(), kind: 'clone', source_rev: 4 } },
      'POST /api/clone': { status: 202, body: applyJob({ kind: 'clone', state: 'running' }) },
    })
    open(`/bulk?mode=clone&source=${ids[0]}`)
    expect(screen.getByRole('radio', { name: '복제' })).toBeChecked()
    expect(screen.getByRole('combobox', { name: '원본 센서' })).toHaveValue(ids[0])
    expect(await screen.findByText(/보통 · 켜진 존 6\/7/)).toBeInTheDocument()

    const sections = screen.getByRole('heading', { name: '복제할 항목' }).closest('section')!
    const checked = within(sections)
      .getAllByRole('checkbox')
      .filter((c) => (c as HTMLInputElement).checked)
      .map((c) => c.closest('label')?.textContent)
    expect(checked).toEqual(['민감도', '존 켜기·끄기', '존 임계값'])
    expect(screen.getByText('존 임계값을 복제하면 각 센서의 보정값이 원본 값으로 바뀝니다')).toBeInTheDocument()

    const targets = screen.getByRole('region', { name: '대상 센서' })
    expect(within(targets).queryByRole('checkbox', { name: /센서 1/ })).not.toBeInTheDocument()
    fireEvent.click(within(targets).getByRole('checkbox', { name: /센서 2/ }))
    fireEvent.click(within(targets).getByRole('checkbox', { name: /센서 3/ }))
    fireEvent.click(screen.getByRole('button', { name: '미리보기 (2대)' }))
    expect(await screen.findByRole('heading', { name: '센서 1의 설정을 복제' })).toBeInTheDocument()
    expect(calls.find((c) => c.path === '/api/clone/preview')?.body).toEqual({
      source: ids[0],
      targets: ids.slice(1),
      sections: ['sensitivity', 'zone_enable', 'zone_thresholds'],
      expect_rev: null,
    })
    fireEvent.click(screen.getByRole('checkbox', { name: '위 센서들의 보정값을 덮어쓰는 것을 확인했습니다' }))
    fireEvent.click(screen.getByRole('button', { name: '복제 (2대)' }))
    await waitFor(() => expect(calls.map((c) => c.path)).toContain('/api/clone'))
    // the source's rev from the preview goes in too (not the older one of the setup's read)
    expect(calls.find((c) => c.path === '/api/clone')?.body).toMatchObject({
      expect_rev: { [ids[0]!]: 4, [ids[1]!]: 0 },
    })
  })
})
