import { afterEach, describe, expect, it, vi } from 'vitest'
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import type { IncomingMessage, ServerResponse } from 'node:http'
import { weatherApiProxy } from './api-proxy'

type Handler = (req: IncomingMessage, res: ServerResponse, next: () => void) => void
const directories: string[] = []
afterEach(() => { vi.unstubAllEnvs(); vi.unstubAllGlobals(); for (const path of directories.splice(0)) rmSync(path, { recursive: true }) })
function setup(key?: string) {
  vi.stubEnv('WEATHER_API_ORIGIN', 'https://api.example.test')
  vi.stubEnv('WEATHER_API_KEY_FILE', '')
  if (key) {
    const directory = mkdtempSync(join(tmpdir(), 'weather-ui-test-'))
    directories.push(directory)
    const path = join(directory, 'key'); writeFileSync(path, key)
    vi.stubEnv('WEATHER_API_KEY_FILE', path)
  }
  let handler: Handler
  const plugin = weatherApiProxy()
  const configure = plugin.configureServer as (server: unknown) => void
  configure({ middlewares: { use: (value: Handler) => { handler = value } } })
  return (url: string, method = 'GET') => new Promise<{ status: number; body: Record<string, unknown> }>(resolve => {
    const response = { statusCode: 200, setHeader() {}, end(body: string) { resolve({ status: this.statusCode, body: JSON.parse(body) }) } }
    handler({ url, method } as IncomingMessage, response as unknown as ServerResponse, () => resolve({ status: 404, body: {} }))
  })
}
describe('local live API adapter', () => {
  it('reports disconnected state without forwarding', async () => {
    const fetcher = vi.fn(); vi.stubGlobal('fetch', fetcher)
    const request = setup()
    expect((await request('/api/connection')).body.configured).toBe(false)
    expect((await request('/api/v1/weather/stations')).status).toBe(503)
    expect(fetcher).not.toHaveBeenCalled()
  })
  it('forwards the explicit read route and preserves unavailable responses', async () => {
    const fetcher = vi.fn().mockResolvedValue({ status: 503, json: async () => ({ detail: 'live_forecast_stale' }) })
    vi.stubGlobal('fetch', fetcher)
    const request = setup('test-only-key')
    const result = await request('/api/v1/weather/stations/42410099999/latest')
    expect(result).toEqual({ status: 503, body: { detail: 'live_forecast_stale' } })
    expect(String(fetcher.mock.calls[0][0])).toBe('https://api.example.test/v1/weather/stations/42410099999/latest')
    expect(fetcher.mock.calls[0][1].headers['x-api-key']).toBe('test-only-key')
    expect((await request('/api/connection')).body).toEqual({ configured: true, origin: 'https://api.example.test' })
  })
  it('does not offer submission or arbitrary proxy routes', async () => {
    const request = setup()
    expect((await request('/api/v1/weather/replays', 'POST')).status).toBe(405)
    expect((await request('/api/anything')).status).toBe(404)
  })
})
