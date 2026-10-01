import { fireEvent, render, screen, within } from '@testing-library/react'
import { Router } from 'wouter'
import { afterEach, describe, expect, it } from 'vitest'
import { App } from '../App'
import { setThreshold } from '../draft'
import { guardNav } from '../navGuard'
import { useDrafts } from '../store/drafts'
import { resetStore } from '../store/store'
import { mockApi } from './api'
import { calibSensors, configView, deviceId, storeState } from './fixtures'

const ID = deviceId(1)
const at = (path: string) => window.history.replaceState(null, '', path)
afterEach(() => at('/'))

// guardNav reads window.location, so the app runs on the browser location (as in main.tsx),
// not on memoryLocation, which leaves window.location where it was.
function openWithDraft() {
  mockApi({})
  at(`/sensors/${ID}/settings`)
  resetStore(storeState({ sensors: calibSensors() }))
  useDrafts.getState().openSensor(configView(1))
  useDrafts.getState().updateSensor(ID, (d) => setThreshold(d, 0, 'trigger', 65))
  render(
    <Router aroundNav={guardNav}>
      <App />
    </Router>,
  )
}

const primaryNav = () => screen.getAllByRole('navigation', { name: '주 메뉴' })[0]!

describe('unsaved-changes guard (15.9.3)', () => {
  it('stays on 머무르기; discards and leaves on 버리고 이동', () => {
    openWithDraft()
    const tab = screen.getByRole('link', { name: /^설정/ })
    expect(tab).toHaveAttribute('aria-current', 'page')
    expect(tab).toHaveTextContent('설정 (바뀜)')

    fireEvent.click(within(primaryNav()).getByRole('link', { name: '대시보드' }))
    expect(window.location.pathname).toBe(`/sensors/${ID}/settings`)
    const dialog = screen.getByRole('dialog')
    expect(within(dialog).getByText('적용하지 않은 변경이 있습니다')).toBeInTheDocument()
    expect(within(dialog).getByText('이 화면을 떠나면 변경 1개를 버립니다')).toBeInTheDocument()
    expect(within(dialog).getByRole('button', { name: '머무르기' })).toHaveFocus()

    fireEvent.click(within(dialog).getByRole('button', { name: '머무르기' }))
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(window.location.pathname).toBe(`/sensors/${ID}/settings`)
    expect(useDrafts.getState().sensors[ID]?.edit.trigger[0]).toBe(65)

    fireEvent.click(within(primaryNav()).getByRole('link', { name: '대시보드' }))
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: '버리고 이동' }))
    expect(window.location.pathname).toBe('/')
    // the old base goes too: coming back reads the device again instead of showing what it was then
    expect(useDrafts.getState().sensors[ID]).toBeUndefined()
  })

  it('moving between the tabs of the same sensor is not held', () => {
    openWithDraft()
    fireEvent.click(screen.getByRole('link', { name: /고급/ }))
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(window.location.pathname).toBe(`/sensors/${ID}/advanced`)
    expect(useDrafts.getState().sensors[ID]?.edit.trigger[0]).toBe(65)
  })

  it('beforeunload is prevented only while something changed', () => {
    openWithDraft()
    const dirty = new Event('beforeunload', { cancelable: true })
    window.dispatchEvent(dirty)
    expect(dirty.defaultPrevented).toBe(true)
    useDrafts.getState().discard(`sensor:${ID}`)
    const clean = new Event('beforeunload', { cancelable: true })
    window.dispatchEvent(clean)
    expect(clean.defaultPrevented).toBe(false)
  })
})
