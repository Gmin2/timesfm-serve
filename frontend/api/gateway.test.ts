import { beforeEach, describe, expect, it, vi } from 'vitest'
import gateway from './gateway.js'

const API = 'https://api.example.test'
const KEY = 'tfm_' + 'a'.repeat(32)

// Vercel rewrites the real route into a __path parameter, so requests arrive
// looking like /api/gateway?__path=v1/iex/scorecard&days=90
function get(path: string, headers: Record<string, string> = {}) {
  const [route, search] = path.split('?')
  const url = new URL('https://dash.example.test/api/gateway')
  url.searchParams.set('__path', route.replace(/^\//, ''))
  for (const [k, v] of new URLSearchParams(search || '')) url.searchParams.append(k, v)
  return gateway.fetch(new Request(url, { headers }))
}

async function forwarded(response: Response): Promise<string> {
  return ((await response.json()) as { forwarded: string }).forwarded
}

beforeEach(() => {
  vi.unstubAllEnvs()
  vi.stubEnv('WEATHER_API_ORIGIN', API)
  vi.stubEnv('WEATHER_DASHBOARD_API_KEY', KEY)
  // forward() calls fetch(url, init), so the stub echoes back what it was given
  vi.stubGlobal('fetch', vi.fn(async (target: URL | string) => {
    const url = new URL(String(target))
    return Response.json({ ok: true, forwarded: url.pathname + url.search })
  }))
})

describe('price routes reach production', () => {
  it.each([
    '/v1/iex/forecast/latest',
    '/v1/iex/forecast/2026-09-15',
    '/v1/iex/scorecard',
    '/v1/iex/models',
  ])('forwards %s', async path => {
    const response = await get(path)
    expect(response.status).toBe(200)
    expect(await forwarded(response)).toBe(path)
  })

  it('keeps the weather routes working alongside them', async () => {
    expect((await get('/v1/weather/stations')).status).toBe(200)
  })
})

describe('the scorecard query is allowed, and only that one', () => {
  it('passes a sane day count through', async () => {
    const response = await get('/v1/iex/scorecard?days=90')
    expect(response.status).toBe(200)
    expect(await forwarded(response)).toBe('/v1/iex/scorecard?days=90')
  })

  it.each(['?days=0', '?days=400', '?days=abc', '?days=90&evil=1', '?other=1'])(
    'rejects %s', async search => {
      expect((await get('/v1/iex/scorecard' + search)).status).toBe(404)
    })

  it('refuses a query on routes that take none', async () => {
    expect((await get('/v1/iex/forecast/latest?days=90')).status).toBe(404)
    expect((await get('/v1/weather/stations?days=1')).status).toBe(404)
  })
})

describe('everything else stays shut', () => {
  it.each([
    '/v1/iex/forecast/not-a-date',
    '/v1/iex/bogus',
    '/v1/iex/forecast/../../etc/passwd',
    '/v1/weather/stations/99999999999/latest',
  ])('rejects %s', async path => {
    expect((await get(path)).status).toBe(404)
  })

  it('sends account routes down their own path, not the read proxy', async () => {
    // no WEATHER_ACCOUNT_ORIGIN is configured here, so it reports unavailable
    // rather than pretending the route does not exist
    expect((await get('/v1/account/keys')).status).toBe(503)
  })

  it('answers connection without forwarding anything', async () => {
    const body = await (await get('/connection')).json() as { configured: boolean; origin: string }
    expect(body).toEqual({ configured: true, origin: API })
    expect(fetch).not.toHaveBeenCalled()
  })

  it('reports not configured when no dashboard key is set', async () => {
    vi.stubEnv('WEATHER_DASHBOARD_API_KEY', '')
    const body = await (await get('/connection')).json() as { configured: boolean }
    expect(body.configured).toBe(false)
    expect((await get('/v1/iex/scorecard')).status).toBe(503)
  })
})

describe('the playground needs a caller supplied key', () => {
  it('forwards a price route with a valid key', async () => {
    const response = await get('/playground/v1/iex/scorecard?days=30', { 'x-api-key': KEY })
    expect(response.status).toBe(200)
    expect(await forwarded(response)).toBe('/v1/iex/scorecard?days=30')
  })

  it('refuses a missing or malformed key', async () => {
    expect((await get('/playground/v1/iex/models')).status).toBe(401)
    expect((await get('/playground/v1/iex/models', { 'x-api-key': 'nope' })).status).toBe(401)
  })
})
