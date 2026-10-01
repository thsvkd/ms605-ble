import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { useState } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ThresholdMeter } from '../components/edit/ThresholdMeter'

const RECT = { left: 0, width: 200, top: 0, right: 200, bottom: 32, height: 32, x: 0, y: 0, toJSON: () => ({}) }

beforeEach(() => {
  vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockReturnValue(RECT as DOMRect)
})

/** base 60, live 58 -> axis 100 (1.25 × 64 = 80); onChange is a spy, so `value` stays put. */
function setup(patch: Partial<Parameters<typeof ThresholdMeter>[0]> = {}) {
  const onChange = vi.fn()
  render(<ThresholdMeter label="재실 트리거" value={60} base={60} live={58} onChange={onChange} {...patch} />)
  return { onChange, slider: screen.getByRole('slider', { name: '재실 트리거' }) }
}

describe('ThresholdMeter: drag', () => {
  it('press anywhere puts the handle there; moves follow; up ends the drag', () => {
    const { onChange, slider } = setup()
    fireEvent.pointerDown(slider, { clientX: 130, button: 0, pointerId: 1 })
    expect(onChange).toHaveBeenLastCalledWith(65)
    expect(slider).toHaveFocus()
    fireEvent.pointerMove(slider, { clientX: 150, pointerId: 1 })
    expect(onChange).toHaveBeenLastCalledWith(75)
    fireEvent.pointerMove(slider, { clientX: 150, pointerId: 1 }) // same value: no call
    expect(onChange).toHaveBeenCalledTimes(2)
    fireEvent.pointerUp(slider, { pointerId: 1 })
    fireEvent.pointerMove(slider, { clientX: 180, pointerId: 1 })
    expect(onChange).toHaveBeenCalledTimes(2)
  })

  it('Escape during a drag goes back to the starting value', () => {
    const { onChange, slider } = setup()
    fireEvent.pointerDown(slider, { clientX: 150, button: 0, pointerId: 1 })
    expect(onChange).toHaveBeenLastCalledWith(75)
    fireEvent.keyDown(slider, { key: 'Escape' })
    expect(onChange).toHaveBeenLastCalledWith(60)
    fireEvent.pointerMove(slider, { clientX: 190, pointerId: 1 })
    expect(onChange).toHaveBeenCalledTimes(2)
  })

  it('pointercancel (the browser took the gesture) puts the starting value back', () => {
    const { onChange, slider } = setup()
    fireEvent.pointerDown(slider, { clientX: 150, button: 0, pointerId: 1 })
    expect(onChange).toHaveBeenLastCalledWith(75)
    fireEvent.pointerCancel(slider, { pointerId: 1 })
    expect(onChange).toHaveBeenLastCalledWith(60)
    fireEvent.pointerMove(slider, { clientX: 190, pointerId: 1 })
    expect(onChange).toHaveBeenCalledTimes(2)
  })

  it('a touch that turns into a vertical scroll never edits', () => {
    const { onChange, slider } = setup()
    fireEvent.pointerDown(slider, { clientX: 150, button: 0, pointerId: 1, pointerType: 'touch' })
    fireEvent.pointerMove(slider, { clientX: 153, clientY: 40, pointerId: 1, pointerType: 'touch' }) // under the slop
    fireEvent.pointerCancel(slider, { pointerId: 1, pointerType: 'touch' })
    expect(onChange).not.toHaveBeenCalled()
    expect(slider).not.toHaveFocus()
  })

  // value 60 on axis 100 over 200 px: the handle sits at x = 120
  it('a touch away from the handle drags once it moves sideways past the slop', () => {
    const { onChange, slider } = setup()
    fireEvent.pointerDown(slider, { clientX: 30, button: 0, pointerId: 1, pointerType: 'touch' })
    fireEvent.pointerMove(slider, { clientX: 37, pointerId: 1, pointerType: 'touch' }) // under the slop
    expect(onChange).not.toHaveBeenCalled()
    fireEvent.pointerMove(slider, { clientX: 40, pointerId: 1, pointerType: 'touch' })
    expect(onChange).toHaveBeenLastCalledWith(20)
    expect(slider).toHaveFocus()
    fireEvent.pointerMove(slider, { clientX: 60, pointerId: 1, pointerType: 'touch' })
    expect(onChange).toHaveBeenLastCalledWith(30)
    fireEvent.pointerUp(slider, { clientX: 60, pointerId: 1, pointerType: 'touch' })
    expect(onChange).toHaveBeenCalledTimes(2)
  })

  it('a touch on the handle drags with any sideways move, by the travel rather than to the finger', () => {
    const { onChange, slider } = setup() // handle centre at x=120 (60 of axis 100 over 200 px)
    fireEvent.pointerDown(slider, { clientX: 125, button: 0, pointerId: 1, pointerType: 'touch' }) // 5 px off-centre
    expect(onChange).not.toHaveBeenCalled()
    fireEvent.pointerMove(slider, { clientX: 128, pointerId: 1, pointerType: 'touch' })
    expect(onChange).toHaveBeenLastCalledWith(62) // 3 px of travel, not a jump to the finger (64)
    fireEvent.pointerMove(slider, { clientX: 150, pointerId: 1, pointerType: 'touch' })
    expect(onChange).toHaveBeenLastCalledWith(73)
    fireEvent.pointerUp(slider, { clientX: 150, pointerId: 1, pointerType: 'touch' })
    expect(onChange).toHaveBeenCalledTimes(2)
  })

  it('a touch tap never changes the value, on the handle or away from it', () => {
    const { onChange, slider } = setup()
    fireEvent.pointerDown(slider, { clientX: 30, button: 0, pointerId: 1, pointerType: 'touch' })
    fireEvent.pointerUp(slider, { clientX: 30, pointerId: 1, pointerType: 'touch' })
    fireEvent.pointerDown(slider, { clientX: 125, button: 0, pointerId: 2, pointerType: 'touch' })
    fireEvent.pointerUp(slider, { clientX: 125, pointerId: 2, pointerType: 'touch' })
    expect(onChange).not.toHaveBeenCalled()
    fireEvent.pointerMove(slider, { clientX: 40, pointerId: 2, pointerType: 'touch' }) // the drag ended with the tap
    expect(onChange).not.toHaveBeenCalled()
  })

  it('ignores a secondary button', () => {
    const { onChange, slider } = setup()
    fireEvent.pointerDown(slider, { clientX: 130, button: 2, pointerId: 1 })
    expect(onChange).not.toHaveBeenCalled()
  })

  it('a disabled slider does not move', () => {
    const { onChange, slider } = setup({ disabled: true })
    expect(slider).toHaveAttribute('aria-disabled', 'true')
    expect(slider).toHaveAttribute('tabindex', '-1')
    fireEvent.pointerDown(slider, { clientX: 130, button: 0, pointerId: 1 })
    fireEvent.keyDown(slider, { key: 'ArrowRight' })
    expect(onChange).not.toHaveBeenCalled()
  })
})

describe('ThresholdMeter: keyboard', () => {
  it.each([
    ['ArrowRight', false, 61],
    ['ArrowUp', false, 61],
    ['ArrowLeft', false, 59],
    ['ArrowRight', true, 70],
    ['PageUp', false, 70],
    ['PageDown', false, 50],
    ['End', false, 500],
    ['Home', false, 0],
  ] as const)('%s (shift %s) -> %s', (key, shiftKey, v) => {
    const { onChange, slider } = setup()
    fireEvent.keyDown(slider, { key, shiftKey })
    expect(onChange).toHaveBeenCalledWith(v)
  })

  it('ArrowLeft does not go below 0; other keys are left alone', () => {
    const { onChange, slider } = setup({ value: 0 })
    fireEvent.keyDown(slider, { key: 'ArrowLeft' })
    fireEvent.keyDown(slider, { key: 'a' })
    expect(onChange).not.toHaveBeenCalled()
  })

  it('keeps moving when the parent holds the value (controlled)', () => {
    function Host() {
      const [v, setV] = useState(60)
      return <ThresholdMeter label="재실 유지" value={v} base={60} live={null} onChange={setV} />
    }
    render(<Host />)
    const slider = screen.getByRole('slider')
    fireEvent.keyDown(slider, { key: 'ArrowRight' })
    fireEvent.keyDown(slider, { key: 'ArrowRight', shiftKey: true })
    expect(slider).toHaveAttribute('aria-valuenow', '71')
  })
})

describe('ThresholdMeter: semantics', () => {
  it('slider values, and 넘음 only when the live value is above the new threshold', () => {
    const { slider } = setup({ value: 64, live: 66 })
    expect(slider).toHaveAttribute('aria-valuemin', '0')
    expect(slider).toHaveAttribute('aria-valuemax', '500')
    expect(slider).toHaveAttribute('aria-valuenow', '64')
    expect(slider).toHaveAttribute('aria-valuetext', '새 임계값 64, 현재 60 — 지금 값이 넘음')
    expect(slider.closest('[data-over]')).toHaveAttribute('data-over', 'true')
    expect(screen.getByText('현재 60 → 새 64', { exact: false })).toBeInTheDocument()
  })

  it('넘지 않음 below, no live part without a frame', () => {
    const { slider } = setup({ value: 64, live: 58 })
    expect(slider).toHaveAttribute('aria-valuetext', '새 임계값 64, 현재 60 — 지금 값이 넘지 않음')
    expect(slider.closest('[data-over]')).toHaveAttribute('data-over', 'false')
  })

  it('without a live frame the text leaves the live part out', () => {
    const { slider } = setup({ live: null })
    expect(slider).toHaveAttribute('aria-valuetext', '새 임계값 60, 현재 60')
  })

  it('the moving live number stays out of aria-valuetext (the caption describes it)', () => {
    const { slider } = setup({ value: 64, live: 30 })
    const text = slider.getAttribute('aria-valuetext')
    cleanup()
    const again = setup({ value: 64, live: 31 }).slider
    expect(again.getAttribute('aria-valuetext')).toBe(text)
    const caption = document.getElementById(again.getAttribute('aria-describedby') ?? '')
    expect(caption).toHaveTextContent('31')
  })

  it('a base above 500 widens max', () => {
    const { slider } = setup({ value: 620, base: 620 })
    expect(slider).toHaveAttribute('aria-valuemax', '620')
  })
})

describe('ThresholdMeter: exact input and reset', () => {
  it('typing + Enter commits; out of range snaps into it', () => {
    const { onChange } = setup()
    const input = screen.getByRole('spinbutton', { name: '재실 트리거 값 입력' })
    fireEvent.change(input, { target: { value: '72' } })
    expect(onChange).not.toHaveBeenCalled() // not while typing
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(onChange).toHaveBeenLastCalledWith(72)
    fireEvent.change(input, { target: { value: '600' } })
    fireEvent.blur(input)
    expect(onChange).toHaveBeenLastCalledWith(500)
    expect(input).toHaveValue(500)
  })

  it('↺ only when changed, and it goes back to the device value', () => {
    setup()
    expect(screen.queryByRole('button', { name: '재실 트리거 되돌리기' })).not.toBeInTheDocument()
    cleanup()
    const { onChange } = setup({ value: 64 })
    fireEvent.click(screen.getByRole('button', { name: '재실 트리거 되돌리기' }))
    expect(onChange).toHaveBeenCalledWith(60)
  })
})
