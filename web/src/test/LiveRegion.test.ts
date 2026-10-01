import { describe, expect, it } from 'vitest'
import { announcements } from '../components/LiveRegion'
import { resetStore, type Store, useStore } from '../store/store'
import { batchView, calibSensors, job, storeState, succeededJob } from './fixtures'

function states(before: Parameters<typeof batchView>[0] | null, after: Parameters<typeof batchView>[0]) {
  resetStore(storeState({ sensors: calibSensors(), batch: before ? batchView(before) : null }))
  const prev: Store = useStore.getState()
  useStore.setState({ batch: batchView(after) })
  return [prev, useStore.getState()] as const
}

describe('announcements: batch (14.8.10)', () => {
  it('says when the round starts running', () => {
    const [prev, next] = states(
      { state: 'waiting', jobs: [job(1), job(2), job(3)] },
      { state: 'running', jobs: [job(1), job(2), job(3)] },
    )
    expect(announcements(prev, next)).toEqual(['보정을 시작했습니다'])
  })

  it('says which sensor dropped, and the totals at the end', () => {
    const running = [job(1, { state: 'learning' }), job(2, { state: 'learning' }), job(3, { state: 'learning' })]
    const [prev, next] = states({ state: 'running', jobs: running }, {})
    expect(announcements(prev, next)).toEqual(['센서 2 보정 중 연결이 끊겼습니다', '보정이 끝났습니다: 성공 2, 실패 1'])
  })

  it('stays quiet on a snapshot', () => {
    const [prev, next] = states(null, { jobs: [succeededJob(1), succeededJob(2), succeededJob(3)] })
    expect(announcements({ ...prev, conn: 'reconnecting' }, next)).toEqual([])
  })
})
