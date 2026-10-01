import { describe, expect, it, vi } from 'vitest'
import { ApiRequestError, createSite, release } from '../api/client'
import { useStore } from '../store/store'

function mockFetch(status: number, body: string, contentType = 'application/json') {
  const fn = vi.fn(async () => new Response(status === 204 ? null : body, { status, headers: { 'Content-Type': contentType } }))
  vi.stubGlobal('fetch', fn)
  return fn
}

describe('api client', () => {
  it('turns an ApiError body into ApiRequestError.code', async () => {
    mockFetch(409, JSON.stringify({ error: { code: 'already_exists', message: 'site exists' } }))
    const err = await createSite('Lab A').catch((e: unknown) => e)
    expect(err).toBeInstanceOf(ApiRequestError)
    expect(err).toMatchObject({ status: 409, code: 'already_exists', message: 'site exists' })
  })

  it('a 401 flips the connection to unauthorized', async () => {
    useStore.setState({ conn: 'open' })
    mockFetch(401, JSON.stringify({ error: { code: 'unauthorized', message: 'no token' } }))
    await expect(createSite('x')).rejects.toMatchObject({ code: 'unauthorized' })
    expect(useStore.getState().conn).toBe('unauthorized')
  })

  it('a non-JSON error body becomes code "internal"', async () => {
    mockFetch(400, 'Invalid host header', 'text/plain')
    await expect(createSite('x')).rejects.toMatchObject({ status: 400, code: 'internal' })
  })

  it('sends JSON with same-origin credentials and handles 204', async () => {
    const fn = mockFetch(204, '')
    await expect(release(null)).resolves.toBeUndefined()
    const [path, init] = fn.mock.calls[0] as unknown as [string, RequestInit]
    expect(path).toBe('/api/release')
    expect(init).toMatchObject({ method: 'POST', credentials: 'same-origin', body: '{"device_ids":null}' })
  })
})
