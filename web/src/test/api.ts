import { vi } from 'vitest'

export type Call = { path: string; method: string; body: unknown }
export type Responses = Record<string, { status: number; body?: unknown } | undefined>

/** fetch -> `${method} ${path}` (path with its query); unknown routes answer 404. Returns the call log. */
export function mockApi(responses: Responses): Call[] {
  const calls: Call[] = []
  vi.stubGlobal(
    'fetch',
    vi.fn(async (path: string, init: RequestInit) => {
      const method = init.method ?? 'GET'
      calls.push({ path, method, body: init.body ? JSON.parse(String(init.body)) : undefined })
      const r = responses[`${method} ${path}`] ?? { status: 404, body: { error: { code: 'not_found', message: '' } } }
      return new Response(r.body === undefined ? null : JSON.stringify(r.body), { status: r.status })
    }),
  )
  return calls
}

export const apiError = (status: number, code: string, message = '') => ({ status, body: { error: { code, message } } })
