import { describe, expect, it } from 'vitest'
import type { BatchView, CalibrationJobView, PresenceView } from '../api/types'
import {
  batchHeadline,
  compareCell,
  formatCountdown,
  formatElapsed,
  type JobContext,
  jobProgress,
  jobStatus,
  presenceStatus,
  resolveAt,
  selectBatchTally,
} from '../calibration'
import { batchView, deviceId, job, NOW_S, succeededJob } from './fixtures'

const ctx = (patch: Partial<JobContext> = {}): JobContext => ({
  batchState: 'done',
  gathering: false,
  link: null,
  ...patch,
})

describe('jobStatus (14.8.9 job table)', () => {
  const rows: [string, CalibrationJobView, Partial<JobContext>, string, string, string | undefined][] = [
    ['idle, waiting', job(1), { batchState: 'waiting' }, 'off', '시작 대기', undefined],
    ['idle, running', job(1), { batchState: 'running' }, 'progress', '시작 준비 중…', undefined],
    ['starting', job(1, { state: 'starting' }), {}, 'progress', '시작하는 중…', undefined],
    ['learning', job(1, { state: 'learning' }), {}, 'progress', '학습 중', '방을 비워 두세요'],
    [
      'succeeded, no readback',
      succeededJob(1, { detail: '반영값 재조회 실패: timeout', after: null }),
      {},
      'warn',
      '완료 (결과값을 읽지 못함)',
      '전후 비교를 할 수 없습니다',
    ],
    [
      'succeeded, not saved',
      succeededJob(1, { detail: '결과 저장 실패: disk', history_saved: false }),
      {},
      'warn',
      '완료 (기록 저장 실패)',
      '보정은 센서에 반영되었습니다',
    ],
    ['succeeded, saved', succeededJob(1), {}, 'ok', '완료', '보정 기록에 저장했습니다'],
    ['succeeded, not saved quietly', succeededJob(1, { history_saved: false }), {}, 'ok', '완료', undefined],
    [
      'failed, busy',
      job(1, { state: 'failed', error: 'busy: identify' }),
      {},
      'warn',
      '시작하지 못함 (다른 작업 중)',
      '잠시 뒤 다시 시도하세요',
    ],
    [
      'failed by the device',
      job(1, { state: 'failed' }),
      {},
      'danger',
      '실패',
      '센서가 보정 실패를 알렸습니다. 방을 비우고 다시 시도하세요',
    ],
    ['failed, error', job(1, { state: 'failed', error: 'write failed' }), {}, 'danger', '실패', '오류: write failed'],
    [
      'lost while learning',
      job(1, { state: 'lost', started: true }),
      {},
      'warn',
      '연결 끊김 (학습 초기화됨)',
      undefined,
    ],
    ['lost before start', job(1, { state: 'lost' }), {}, 'warn', '연결 끊김 (시작 전)', undefined],
    [
      'timeout',
      job(1, { state: 'timeout' }),
      {},
      'danger',
      '응답 없음',
      '센서가 완료를 알리지 않았습니다. 다시 시도하세요',
    ],
    [
      'cancelled while learning',
      job(1, { state: 'cancelled', started: true }),
      {},
      'off',
      '취소됨',
      '연결을 끊어 학습을 멈췄습니다. 다시 연결하려면 버튼을 누르세요',
    ],
    ['cancelled', job(1, { state: 'cancelled' }), {}, 'off', '취소됨', undefined],
  ]

  it.each(rows)('%s', (_name, j, c, kind, label, hint) => {
    const s = jobStatus(j, ctx(c))
    expect(s.kind).toBe(kind)
    expect(s.label).toBe(label)
    expect(s.label).not.toBe('')
    if (hint !== undefined) expect(s.hint).toBe(hint)
    else if (j.state !== 'lost') expect(s.hint).toBeUndefined()
  })

  it('picks the LOST hint from the batch, the link and gathering', () => {
    const lost = job(1, { state: 'lost', started: true })
    expect(jobStatus(lost, ctx({ batchState: 'running', link: 'connected' })).hint).toBe(
      '이번 보정이 끝난 뒤 다시 시도할 수 있습니다',
    )
    expect(jobStatus(lost, ctx({ link: 'connected' })).hint).toBe('다시 연결됨 · 다시 시도할 수 있습니다')
    expect(jobStatus(lost, ctx({ link: 'lost', gathering: true })).hint).toBe('버튼을 다시 누르세요')
    expect(jobStatus(lost, ctx({ link: 'lost' })).hint).toBe('센서 모으기를 켜고 버튼을 다시 누르세요')
  })
})

describe('presenceStatus', () => {
  const p = (patch: Partial<PresenceView>): PresenceView => ({
    device_id: deviceId(1),
    samples: 3,
    presence: false,
    pir: false,
    occupied: false,
    error: null,
    ...patch,
  })

  it('occupied names its reasons', () => {
    expect(presenceStatus(p({ occupied: true, presence: true, pir: true }))).toMatchObject({
      kind: 'warn',
      label: '아직 사람 있음',
      hint: '근거: 센서 재실·PIR 감지',
    })
    expect(presenceStatus(p({ occupied: true, presence: false, pir: true })).hint).toBe('근거: PIR 감지')
    expect(presenceStatus(p({ occupied: true, presence: true, pir: null })).hint).toBe('근거: 센서 재실')
  })

  it('empty, error and no samples', () => {
    expect(presenceStatus(p({}))).toMatchObject({ kind: 'ok', label: '비어 있음' })
    expect(
      presenceStatus(p({ occupied: null, presence: null, pir: null, samples: 0, error: 'not connected' })),
    ).toMatchObject({ kind: 'off', label: '확인하지 못함', hint: 'not connected' })
    expect(presenceStatus(p({ occupied: null, presence: null, pir: null, samples: 0 }))).toMatchObject({
      kind: 'off',
      label: '확인하지 못함',
      hint: '값을 받지 못했습니다',
    })
  })
})

describe('jobProgress', () => {
  it('is elapsed / expected while learning, capped at 99%', () => {
    expect(jobProgress(job(1, { state: 'learning', elapsed_s: 90 }), 180)).toEqual({ ratio: 0.5, overdue: false })
    expect(jobProgress(job(1, { state: 'learning', elapsed_s: 400 }), 180)).toEqual({ ratio: 0.99, overdue: true })
    expect(jobProgress(job(1, { state: 'learning', elapsed_s: null }), 180)).toEqual({ ratio: 0, overdue: false })
  })

  it('is 1 once succeeded and absent otherwise', () => {
    expect(jobProgress(succeededJob(1), 180).ratio).toBe(1)
    for (const state of ['idle', 'starting', 'failed', 'lost', 'timeout', 'cancelled'] as const) {
      expect(jobProgress(job(1, { state, elapsed_s: 10 }), 180).ratio).toBeNull()
    }
  })
})

describe('time formatting', () => {
  it('formatCountdown rounds up', () => {
    expect(formatCountdown(59.2)).toBe('1:00')
    expect(formatCountdown(42)).toBe('0:42')
    expect(formatCountdown(0)).toBe('0:00')
    expect(formatCountdown(3600)).toBe('1:00:00')
    expect(formatCountdown(3725)).toBe('1:02:05')
  })

  it('formatElapsed rounds down', () => {
    expect(formatElapsed(72.9)).toBe('1:12')
    expect(formatElapsed(180)).toBe('3:00')
  })

  it('resolveAt: later today, else tomorrow, across midnight', () => {
    const now = new Date(2026, 9, 1, 13, 0, 0)
    expect(resolveAt('14:30', now)).toEqual(new Date(2026, 9, 1, 14, 30, 0))
    expect(resolveAt('07:00', now)).toEqual(new Date(2026, 9, 2, 7, 0, 0))
    expect(resolveAt('13:00', now)).toEqual(new Date(2026, 9, 2, 13, 0, 0))
    const late = new Date(2026, 9, 31, 23, 50, 0)
    expect(resolveAt('00:10', late)).toEqual(new Date(2026, 10, 1, 0, 10, 0))
  })

  it('compareCell uses a real minus, a plus and (0)', () => {
    expect(compareCell(70, 64)).toBe('70 → 64 (−6)')
    expect(compareCell(62, 66)).toBe('62 → 66 (+4)')
    expect(compareCell(30, 30)).toBe('30 → 30 (0)')
  })
})

describe('batchHeadline (14.8.9 batch table)', () => {
  const now = new Date(NOW_S * 1000)
  const b = (patch: Partial<BatchView>) => batchView(patch)

  it('waiting: now / delay / at', () => {
    expect(batchHeadline(b({ state: 'waiting', start: 'now' }), 0, now)).toBe('시작하는 중…')
    expect(batchHeadline(b({ state: 'waiting', start: 'delay' }), 41.3, now)).toBe('0:42 후 보정 시작')
    const fire = new Date(NOW_S * 1000 + (2 * 3600 + 3 * 60 + 20) * 1000)
    const hhmm = `${String(fire.getHours()).padStart(2, '0')}:${String(fire.getMinutes()).padStart(2, '0')}`
    expect(batchHeadline(b({ state: 'waiting', start: 'at', fire_at: fire.getTime() / 1000 }), null, now)).toBe(
      `${hhmm}에 보정 시작 · 2시간 3분 남음`,
    )
    expect(batchHeadline(b({ state: 'waiting', start: 'at' }), 12 * 60 + 5, now)).toMatch(/· 12분 남음$/)
    expect(batchHeadline(b({ state: 'waiting', start: 'at' }), 30, now)).toMatch(/· 1분 미만 남음$/)
  })

  it('running counts the round, done the whole batch', () => {
    const running = b({
      state: 'running',
      jobs: [succeededJob(1), job(2, { state: 'learning' }), job(3, { state: 'lost', retryable: true })],
    })
    expect(batchHeadline(running, null, now)).toBe('보정 중 · 2/3 끝남')
    const retryRound = b({ state: 'running', round: 2, round_ids: [deviceId(2)], jobs: running.jobs })
    expect(batchHeadline(retryRound, null, now)).toBe('보정 중 · 0/1 끝남')
    expect(batchHeadline(b({}), null, now)).toBe('보정 끝 · 성공 2 · 실패 1')
    expect(batchHeadline(b({ state: 'cancelled' }), null, now)).toBe('보정을 취소했습니다')
  })

  it('selectBatchTally counts failed, lost and timeout as failed', () => {
    const tally = selectBatchTally(
      b({ jobs: [job(1, { state: 'failed' }), job(2, { state: 'timeout' }), succeededJob(3)] }),
    )
    expect(tally).toEqual({ ended: 3, total: 3, succeeded: 1, failed: 2 })
  })
})
