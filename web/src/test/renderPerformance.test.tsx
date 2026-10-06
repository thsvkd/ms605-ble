import { act, render } from '@testing-library/react'
import { Profiler, type ProfilerOnRenderCallback } from 'react'
import { describe, expect, it } from 'vitest'
import { MonitorSummary } from '../components/MonitorWall'
import { SensorCard } from '../components/SensorCard'
import { resetStore, useStore } from '../store/store'
import { deviceId, live, liveData, NOW_S, registry, sensor, SITE_A, storeState } from './fixtures'

const SENSOR_COUNT = 13
const ids = Array.from({ length: SENSOR_COUNT }, (_, i) => deviceId(i + 1))

/** A live frame whose displayed presence is unchanged, while its radar values keep moving. */
function radarOnlyFrame(n: number, step: number) {
  const frame = liveData(n)
  return {
    ...frame,
    at: NOW_S + step,
    zones: frame.zones.map((zone) => ({
      ...zone,
      trigger: zone.trigger + step,
      maintain: zone.maintain + step,
    })),
  }
}

function seedSensors() {
  const sensors = ids.map((_, i) =>
    sensor(i + 1, { registry: registry(SITE_A, `센서 ${i + 1}`), live: live(i + 1) }),
  )
  const frames = Object.fromEntries(ids.map((id, i) => [id, radarOnlyFrame(i + 1, 0)]))
  const watch = Object.fromEntries(ids.map((id) => [id, 1]))
  resetStore({ ...storeState({ sites: [SITE_A], sensors }), live: frames, watch })
}

function renderMeasured() {
  const updates = { cardUpdates: 0, summaryUpdates: 0 }
  const countCardUpdates: ProfilerOnRenderCallback = (_id, phase) => {
    if (phase !== 'mount') updates.cardUpdates += 1
  }
  const countSummaryUpdates: ProfilerOnRenderCallback = (_id, phase) => {
    if (phase !== 'mount') updates.summaryUpdates += 1
  }

  render(
    <>
      {ids.map((id) => (
        <Profiler key={id} id={`card-${id}`} onRender={countCardUpdates}>
          <SensorCard deviceId={id} />
        </Profiler>
      ))}
      <Profiler id="monitor-summary" onRender={countSummaryUpdates}>
        <MonitorSummary ids={ids} />
      </Profiler>
    </>,
  )
  return updates
}

describe('live rendering with 13 sensors', () => {
  it('does not redraw dashboard cards or the monitor summary for radar-only frame changes', () => {
    seedSensors()
    const updates = renderMeasured()

    ids.forEach((_, i) => {
      act(() => {
        useStore.getState().applyMessage({
          type: 'live',
          seq: null,
          ts: NOW_S + i + 1,
          data: radarOnlyFrame(i + 1, i + 1),
        })
      })
    })

    expect(updates).toEqual({ cardUpdates: 0, summaryUpdates: 0 })
  })

  it('redraws immediately when a displayed signal or connection state really changes', () => {
    seedSensors()
    const updates = renderMeasured()

    act(() => {
      useStore.getState().applyMessage({
        type: 'live', seq: null, ts: NOW_S + 1, data: liveData(1, { pir: false }),
      })
    })
    act(() => {
      const frame = liveData(2)
      useStore.getState().applyMessage({
        type: 'live',
        seq: null,
        ts: NOW_S + 2,
        data: { ...frame, zones: frame.zones.map((zone) => ({ ...zone, trigger_active: false })) },
      })
    })
    act(() => {
      useStore.getState().applyMessage({
        type: 'live',
        seq: null,
        ts: NOW_S + 3,
        data: liveData(3, { sub_sensor_presence: [false, true, false] }),
      })
    })
    act(() => {
      const state = useStore.getState()
      const view = state.sensors[deviceId(4)]!
      useStore.setState({
        sensors: {
          ...state.sensors,
          [view.device_id]: { ...view, live: { ...view.live!, link: 'disconnected' } },
        },
      })
    })

    // S1..S3 changes redraw the card even when the overall presence tally stays the same.
    expect(updates).toEqual({ cardUpdates: 4, summaryUpdates: 3 })
  })
})
