import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { SensorInfoList } from '../components/SensorInfoList'
import { live, sensor } from './fixtures'

describe('SensorInfoList', () => {
  it('shows the device MAC separately from the host BLE address', () => {
    render(<SensorInfoList sensor={sensor(1, {
      live: live(1, { mac: '84:CC:A8:12:34:56', address: '00000000-0000-4000-8000-000000000001' }),
    })} />)

    expect(screen.getByText('MAC').nextElementSibling).toHaveTextContent('84:CC:A8:12:34:56')
    expect(screen.getByText('주소').nextElementSibling).toHaveTextContent('00000000-0000-4000-8000-000000000001')
  })

  it('keeps old server responses without a MAC compatible', () => {
    render(<SensorInfoList sensor={sensor(1, { live: live(1) })} />)

    expect(screen.getByText('MAC').nextElementSibling).toHaveTextContent('—')
  })
})
