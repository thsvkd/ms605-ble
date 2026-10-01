import { renderHook } from '@testing-library/react'
import { StrictMode } from 'react'
import { describe, expect, it } from 'vitest'
import { useLiveWatch } from '../hooks/useLiveWatch'
import { resetStore, useStore } from '../store/store'
import { deviceId, storeState } from './fixtures'

const [a, b, c] = [deviceId(1), deviceId(2), deviceId(3)]

describe('useLiveWatch', () => {
  it('applies only the difference when the list changes, so a kept id never drops to 0', () => {
    resetStore(storeState())
    const { rerender, unmount } = renderHook(({ ids }) => useLiveWatch(ids), {
      initialProps: { ids: [a, b] },
      wrapper: StrictMode, // as main.tsx: the effects run twice on mount
    })
    expect(useStore.getState().watch).toEqual({ [a]: 1, [b]: 1 })

    const seen: Record<string, number>[] = []
    const stop = useStore.subscribe((s) => seen.push(s.watch))
    rerender({ ids: [b, c, a] }) // c joins
    rerender({ ids: [c, a] }) // b leaves
    stop()
    expect(seen.every((w) => w[a] === 1)).toBe(true)
    expect(useStore.getState().watch).toEqual({ [a]: 1, [c]: 1 })

    unmount()
    expect(useStore.getState().watch).toEqual({})
  })

  it('shares the refcount with another view of the same sensor', () => {
    resetStore(storeState())
    const one = renderHook(() => useLiveWatch([a]))
    const two = renderHook(({ ids }) => useLiveWatch(ids), { initialProps: { ids: [a] } })
    expect(useStore.getState().watch).toEqual({ [a]: 2 })
    two.rerender({ ids: [] })
    expect(useStore.getState().watch).toEqual({ [a]: 1 })
    two.unmount()
    one.unmount()
    expect(useStore.getState().watch).toEqual({})
  })
})
