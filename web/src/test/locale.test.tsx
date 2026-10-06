import { act, render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { App } from '../App'
import { BluetoothError } from '../api/bluetooth'
import { failureText } from '../api/client'
import { GatherHero } from '../components/GatherHero'
import { relativeTime } from '../format'
import { en } from '../locales/en'
import { ko } from '../locales/ko'
import { sensorStatus } from '../status'
import { errorText, getLocale, initializeLocale, readLocale, setLocale } from '../strings'
import { resetStore } from '../store/store'
import { deviceId, live, registry, sensor, SITE_A, storeState } from './fixtures'

const STORAGE_KEY = 'ms605.language'

function mockLanguages(languages: readonly string[], language = languages[0] ?? '') {
  vi.spyOn(window.navigator, 'languages', 'get').mockReturnValue(languages as string[])
  vi.spyOn(window.navigator, 'language', 'get').mockReturnValue(language)
}

function expectSameCatalogShape(left: unknown, right: unknown, path = 'catalog'): void {
  expect(typeof right, path).toBe(typeof left)
  if (typeof left !== 'object' || left === null || typeof right !== 'object' || right === null) return
  expect(Object.keys(right), path).toEqual(Object.keys(left))
  for (const key of Object.keys(left)) {
    expectSameCatalogShape(
      (left as Record<string, unknown>)[key],
      (right as Record<string, unknown>)[key],
      `${path}.${key}`,
    )
  }
}

function mockApiError(code: string, status = 500): void {
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => new Response(JSON.stringify({ error: { code, message: 'test detail' } }), { status })),
  )
}

afterEach(() => {
  window.history.replaceState({}, '', '/')
})

describe('locale selection', () => {
  it('prefers a saved choice over the browser languages', () => {
    localStorage.setItem(STORAGE_KEY, 'en')
    mockLanguages(['ko-KR'])

    expect(readLocale()).toBe('en')
    initializeLocale()
    expect(getLocale()).toBe('en')
    expect(document.documentElement.lang).toBe('en')
  })

  it('uses the first supported browser language and otherwise falls back to English', () => {
    mockLanguages(['fr-FR', 'ko-KR', 'en-US'])
    expect(readLocale()).toBe('ko')

    vi.restoreAllMocks()
    mockLanguages(['fr-FR', 'de-DE'])
    expect(readLocale()).toBe('en')
  })

  it('keeps working in memory when localStorage is blocked', () => {
    mockLanguages(['en-GB'])
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => { throw new DOMException('blocked') })
    expect(readLocale()).toBe('en')

    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new DOMException('blocked') })
    expect(() => setLocale('en')).not.toThrow()
    expect(getLocale()).toBe('en')
    expect(document.documentElement.lang).toBe('en')
  })
})

describe('translation catalogs and non-React formatters', () => {
  it('keeps the English catalog complete and interpolation values usable', () => {
    expectSameCatalogShape(ko, en)
    expect(en.gather.connecting(1)).toBe('Connecting to 1 sensor…')
    expect(en.gather.connecting(2)).toBe('Connecting to 2 sensors…')
    expect(en.sensor.macAlias('A1B2C3')).toBe('MS605-A1B2C3')
  })

  it('switches time, status, and error text to English', () => {
    setLocale('en')
    const now = Date.parse('2026-10-01T12:00:00Z')

    expect(relativeTime('2026-10-01T11:55:00Z', now)).toBe('5 min ago')
    expect(sensorStatus(sensor(1, { live: live(1) }), false, now).label).toBe('Connected')
    expect(errorText('invalid_file', 'bad yaml')).toBe('Invalid file format (bad yaml)')
  })

  it('maps a retained Bluetooth error through the currently selected catalog', () => {
    const error = new BluetoothError('permission')
    expect(failureText(error)).toBe(ko.bluetooth.permission)
    setLocale('en')
    expect(failureText(error)).toBe(en.bluetooth.permission)
  })

  it('describes the computer whose Bluetooth is used in each English transport mode', () => {
    setLocale('en')
    resetStore(storeState({ server: { version: 'test', lan: true, sim: null, ble_transport: 'server' } }))
    const { rerender } = render(<GatherHero compact={false} wide={false} />)
    expect(screen.getByText(en.bluetooth.server)).toHaveTextContent('computer running the web app server')

    act(() => {
      resetStore(storeState({ server: { version: 'test', lan: true, sim: null, ble_transport: 'browser' } }))
    })
    rerender(<GatherHero compact={false} wide={false} />)
    expect(screen.getByText(en.bluetooth.client)).toHaveTextContent('device running this browser')
  })
})

describe('language control', () => {
  it('switches the authorization screen immediately and saves the choice', async () => {
    const user = userEvent.setup()
    resetStore({ ...storeState(), conn: 'unauthorized' })
    render(<App />)

    expect(screen.getByRole('heading', { name: '접속 권한이 없습니다' })).toBeInTheDocument()
    const language = screen.getByRole('combobox', { name: '언어' })
    await user.selectOptions(language, 'en')

    expect(screen.getByRole('heading', { name: 'Access denied' })).toBeInTheDocument()
    expect(screen.getByRole('combobox', { name: 'Language' })).toHaveValue('en')
    expect(localStorage.getItem(STORAGE_KEY)).toBe('en')
    expect(document.documentElement.lang).toBe('en')
  })

  it('preserves a sensor registration draft while switching Korean to English and back', async () => {
    const user = userEvent.setup()
    window.history.replaceState({}, '', '/gather')
    resetStore(storeState({
      sites: [SITE_A],
      gather: { gathering: true, connecting: [] },
      sensors: [sensor(1, { live: live(1, { mac: '84:CC:A8:12:34:56' }) })],
    }))
    render(<App />)

    const form = screen.getByRole('form', { name: /센서 등록/ })
    const alias = within(form).getByLabelText('이름')
    const location = within(form).getByLabelText('위치')
    await user.clear(alias)
    await user.type(alias, '회의실 센서')
    await user.type(location, '북쪽 벽')

    const language = screen.getByRole('combobox', { name: '언어' })
    await user.selectOptions(language, 'en')
    const englishForm = screen.getByRole('form', { name: /Register sensor/ })
    expect(within(englishForm).getByLabelText('Name')).toHaveValue('회의실 센서')
    expect(within(englishForm).getByLabelText('Location')).toHaveValue('북쪽 벽')
    expect(screen.getAllByRole('navigation', { name: 'Main menu' }).length).toBeGreaterThan(0)

    await user.selectOptions(language, 'ko')
    const koreanForm = screen.getByRole('form', { name: /센서 등록/ })
    expect(within(koreanForm).getByLabelText('이름')).toHaveValue('회의실 센서')
    expect(within(koreanForm).getByLabelText('위치')).toHaveValue('북쪽 벽')
  })

  it('retranslates a gather request error that is already visible', async () => {
    const user = userEvent.setup()
    window.history.replaceState({}, '', '/gather')
    resetStore(storeState())
    mockApiError('internal')
    render(<App />)

    await user.click(screen.getByRole('button', { name: '센서 추가 시작' }))
    expect(await screen.findByRole('alert')).toHaveTextContent(ko.error.internal)

    await user.selectOptions(screen.getByRole('combobox', { name: '언어' }), 'en')
    expect(screen.getByRole('alert')).toHaveTextContent(en.error.internal)
  })

  it('retranslates an existing config read error without retrying the request', async () => {
    const user = userEvent.setup()
    const id = deviceId(1)
    window.history.replaceState({}, '', `/sensors/${id}/settings`)
    resetStore(storeState({
      sites: [SITE_A],
      sensors: [sensor(1, { registry: registry(SITE_A, '회의실 센서'), live: live(1) })],
    }))
    mockApiError('device_error', 502)
    render(<App />)

    expect(await screen.findByRole('alert')).toHaveTextContent(ko.error.device_error)
    expect(fetch).toHaveBeenCalledTimes(1)
    await user.selectOptions(screen.getByRole('combobox', { name: '언어' }), 'en')

    expect(screen.getByRole('alert')).toHaveTextContent(en.error.device_error)
    expect(screen.getByText('회의실 센서')).toBeInTheDocument()
    expect(fetch).toHaveBeenCalledTimes(1)
  })
})
