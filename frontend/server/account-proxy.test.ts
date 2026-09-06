import { Readable } from 'node:stream'
import type { IncomingMessage, ServerResponse } from 'node:http'
import { afterEach, expect, it, vi } from 'vitest'
import { accountProxy } from './account-proxy'

afterEach(() => { vi.unstubAllEnvs(); vi.unstubAllGlobals() })
function setup(origin = 'https://account.example.test') {
  vi.stubEnv('WEATHER_ACCOUNT_ORIGIN', origin)
  let handler: (req: IncomingMessage, res: ServerResponse, next: () => void) => Promise<void>
  const configure = accountProxy().configureServer as (server: unknown) => void
  configure({ middlewares: { use: (value: typeof handler) => { handler = value } } })
  return (url: string, method = 'GET', body = '', headers: Record<string, string> = {}) => new Promise<{ status: number; body: string; headers: Record<string, unknown> }>(resolve => {
    const req = Object.assign(Readable.from(body ? [body] : []), { url, method, headers }) as IncomingMessage
    const resultHeaders: Record<string, unknown> = {}
    const response = { statusCode: 200, setHeader(name: string, value: unknown) { resultHeaders[name] = value },
      end(value: unknown) { resolve({ status: this.statusCode, body: String(value), headers: resultHeaders }) } }
    void handler(req, response as unknown as ServerResponse, () => resolve({ status: 404, body: '', headers: {} }))
  })
}

it('shows unconfigured login without inventing a user', async () => {
  const result = await setup('')('/api/auth/session')
  expect(JSON.parse(result.body)).toMatchObject({ configured: false, user: null, csrf_token: null })
})

it('forwards cookies, CSRF and redirects without an operator key', async () => {
  const fetcher = vi.fn().mockResolvedValue(new Response(null, { status: 303, headers: {
    Location: 'https://github.com/login/oauth/authorize?state=test', 'Set-Cookie': 'weather_oauth=test; HttpOnly; SameSite=Lax; Path=/',
  } }))
  vi.stubGlobal('fetch', fetcher)
  const result = await setup()('/api/auth/github', 'GET', '', { cookie: 'weather_session=test', 'x-api-key': 'must-not-forward' })
  expect(result.status).toBe(303)
  expect(result.headers.Location).toBeUndefined()
  expect(result.headers.location).toContain('https://github.com/')
  expect(result.headers['Set-Cookie']).toHaveLength(1)
  expect(fetcher.mock.calls[0][1].redirect).toBe('manual')
  expect(fetcher.mock.calls[0][1].headers).toEqual({ Accept: 'application/json', cookie: 'weather_session=test' })
})

it('preserves key creation response and scopes forwarded routes', async () => {
  const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify({ key: 'test-only-key' }), { status: 201 }))
  vi.stubGlobal('fetch', fetcher)
  const request = setup()
  const result = await request('/api/v1/account/keys', 'POST', '{"label":"Example"}', { origin: 'https://ui.example.test', 'x-csrf-token': 'csrf' })
  expect(result.status).toBe(201)
  expect(fetcher.mock.calls[0][1].headers.origin).toBe('https://ui.example.test')
  expect((await request('/api/v1/account/admin')).status).toBe(404)
  expect((await request('/api/auth/session', 'POST')).status).toBe(405)
  expect((await request('/api/v1/account/keys', 'POST', 'x'.repeat(8193))).status).toBe(413)
  expect(fetcher).toHaveBeenCalledTimes(1)
})

it('redacts upstream errors', async () => {
  vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('private-host-and-secret')))
  const result = await setup()('/api/auth/session')
  expect(result.status).toBe(502)
  expect(result.body).not.toContain('private-host')
})
