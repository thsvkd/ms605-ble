import { render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it } from 'vitest'
import { SensorTabs } from '../components/edit/SensorTabs'
import type { ConfigView } from '../api/types'
import { emptyEdit } from '../draft'
import { resetDrafts, useDrafts } from '../store/drafts'

const ID = 'aa'.repeat(20)

function withEdit(patch: Partial<ReturnType<typeof emptyEdit>>) {
  const edit = { ...emptyEdit(), ...patch }
  useDrafts.setState({ sensors: { [ID]: { deviceId: ID, base: {} as ConfigView, edit } } })
  render(<SensorTabs deviceId={ID} current="settings" />)
}

const dotted = (name: RegExp) => screen.getByRole('link', { name }).textContent?.includes('바뀜') ?? false

describe('SensorTabs', () => {
  beforeEach(resetDrafts)

  it('marks only 설정 when a threshold changed', () => {
    const trigger = Array(7).fill(null)
    trigger[0] = 93
    withEdit({ trigger })
    expect(dotted(/설정/)).toBe(true)
    expect(dotted(/고급/)).toBe(false)
  })

  it('marks only 고급 when DND changed', () => {
    withEdit({ dnd: true })
    expect(dotted(/고급/)).toBe(true)
    expect(dotted(/설정/)).toBe(false)
  })
})
