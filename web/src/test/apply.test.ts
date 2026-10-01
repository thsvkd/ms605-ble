import { describe, expect, it } from 'vitest'
import type { ApplyKind, Change, RiskCode } from '../api/types'
import {
  applyHeadline,
  applyItemStatus,
  canRollback,
  formatChange,
  needsOverwriteAck,
  riskText,
  rowLabel,
  snapshotTime,
} from '../apply'
import { applyItem, applyJob, change, previewPlain, previewRisky, SNAP_1 } from './fixtures'

describe('applyItemStatus (15.9.16 table)', () => {
  const rows: [string, Parameters<typeof applyItem>[1], ApplyKind, string, string, string | undefined][] = [
    ['queued', { state: 'queued' }, 'apply', 'off', '대기', undefined],
    ['applying', { state: 'applying' }, 'apply', 'progress', '적용 중…', '쓰고 다시 읽어 확인합니다 (최대 3초)'],
    ['verified + skipped', { skipped: ['detect_mode'] }, 'clone', 'ok', '적용됨 · 확인함', '건너뜀: 감지 모드'],
    ['verified rollback', {}, 'rollback', 'ok', '되돌림 · 확인함', undefined],
    ['verified', {}, 'apply', 'ok', '적용됨 · 확인함', undefined],
    ['partial', { state: 'partial', mismatched: ['zone_thresholds'] }, 'apply', 'warn', '일부만 반영됨', '반영 안 됨: 존 임계값. 되돌리기를 권합니다'],
    ['unverified', { state: 'unverified' }, 'apply', 'warn', '적용했지만 확인하지 못함', '다시 읽기에 실패했습니다. 설정 탭에서 값을 확인하세요'],
    ['busy', { state: 'failed', snapshot: null, error: 'busy: read' }, 'apply', 'warn', '시작하지 못함 (다른 작업 중)', '바뀐 것은 없습니다. 잠시 뒤 새 초안으로 다시 하세요'],
    ['lost', { state: 'failed', snapshot: null, error: 'not connected' }, 'apply', 'warn', '연결 끊김', '바뀐 것은 없습니다. 센서 버튼을 누르고 다시 하세요'],
    ['failed clean', { state: 'failed', snapshot: null, error: 'boom' }, 'apply', 'danger', '실패 (바뀐 것 없음)', '오류: boom'],
    ['failed write', { state: 'failed', error: 'status 5' }, 'apply', 'danger', '실패 (쓰는 중)', '일부가 바뀌었을 수 있습니다. 되돌리기로 이전 값을 복원하세요 · 오류: status 5'],
  ]
  it.each(rows)('%s', (_, patch, kind, k, label, hint) => {
    const s = applyItemStatus(applyItem(1, patch), kind)
    expect(s.kind).toBe(k)
    expect(s.label).toBe(label)
    expect(s.label).not.toBe('')
    expect(s.hint).toBe(hint)
  })
})

describe('applyHeadline', () => {
  it('running: kind and ended/total', () => {
    const job = applyJob({ state: 'running', items: [applyItem(1), applyItem(2, { state: 'applying' }), applyItem(3, { state: 'queued' })] })
    expect(applyHeadline(job)).toBe('설정 적용 중 · 1/3')
  })

  it('done, all verified', () => {
    expect(applyHeadline(applyJob({ kind: 'clone' }))).toBe('복제 끝 · 모두 확인함 (3대)')
  })

  it('done, mixed: zero counts drop out but 확인함 stays', () => {
    const job = applyJob({ items: [applyItem(1, { state: 'failed' }), applyItem(2, { state: 'partial' })] })
    expect(applyHeadline(job)).toBe('설정 적용 끝 · 확인함 0 · 일부 1 · 실패 1')
  })
})

describe('rowLabel', () => {
  const cases: [Partial<Change>, string][] = [
    [{ section: 'sensitivity', index: null, part: 'value' }, '민감도'],
    [{ section: 'detect_mode', index: null, part: 'value' }, '감지 모드'],
    [{ section: 'dnd', index: null, part: 'value' }, '방해 금지'],
    [{ section: 'zone_enable', index: 3, part: 'value' }, 'Z3 켜기/끄기'],
    [{ section: 'zone_thresholds', index: 0, part: 'trigger' }, 'Z0 재실 트리거'],
    [{ section: 'zone_thresholds', index: 6, part: 'maintain' }, 'Z6 재실 유지'],
    [{ section: 'subsensor_zones', index: 0, part: 'value' }, 'S1 구역'],
    [{ section: 'subsensor_timing', index: 1, part: 'presence_s' }, 'S2 재실 유지 시간'],
    [{ section: 'subsensor_timing', index: 2, part: 'absence_s' }, 'S3 부재 판정 시간'],
    [{ section: 'subsensor_enable', index: 2, part: 'value' }, 'S3 사용'],
  ]
  it.each(cases)('%o -> %s', (patch, label) => {
    expect(rowLabel(change(patch))).toBe(label)
  })
})

describe('formatChange', () => {
  it.each([
    [change({ before: 70, after: 75 }), '70 → 75 (+5)'],
    [change({ part: 'maintain', before: 40, after: 18 }), '40 → 18 (−22)'],
    [change({ section: 'zone_enable', part: 'value', before: true, after: false }), '켜짐 → 꺼짐'],
    [change({ section: 'subsensor_zones', part: 'value', before: [0, 1], after: [] }), 'Z0, Z1 → 없음'],
    [change({ section: 'dnd', index: null, part: 'value', before: null, after: true }), '알 수 없음 → 켜짐'],
    [change({ section: 'subsensor_timing', part: 'presence_s', before: 5, after: 10 }), '5초 → 10초 (+5)'],
    [change({ section: 'sensitivity', index: null, part: 'value', before: 2, after: 3 }), '보통 → 높음'],
    [change({ section: 'detect_mode', index: null, part: 'value', before: 1, after: 4 }), '레이더 → 공간 학습'],
  ])('%#: %s', (c, text) => {
    expect(formatChange(c)).toBe(text)
  })
})

describe('risks and rollback', () => {
  const codes: RiskCode[] = [
    'absolute_overwrite',
    'large_change',
    'beyond_ui_range',
    'zone_off',
    'subsensor_off',
    'subsensor_no_zone',
    'sensitivity_only',
    'dnd_on',
    'learning_skipped',
  ]
  it.each(codes)('%s has words', (code) => {
    expect(riskText(code).length).toBeGreaterThan(0)
  })

  it('canRollback: a snapshot and an ended state', () => {
    expect(canRollback(applyItem(1))).toBe(true)
    expect(canRollback(applyItem(1, { state: 'failed' }))).toBe(true)
    expect(canRollback(applyItem(1, { state: 'failed', snapshot: null }))).toBe(false)
    expect(canRollback(applyItem(1, { state: 'applying' }))).toBe(false)
    expect(canRollback(applyItem(1, { state: 'queued' }))).toBe(false)
  })

  it('needsOverwriteAck only for absolute_overwrite', () => {
    expect(needsOverwriteAck(previewRisky())).toBe(true)
    expect(needsOverwriteAck(previewPlain())).toBe(false)
  })

  it('snapshotTime reads the storage name as UTC', () => {
    expect(new Date(snapshotTime(SNAP_1)).toISOString()).toBe('2026-10-01T08:00:00.000Z')
    expect(snapshotTime('nope')).toBeNaN()
  })
})
