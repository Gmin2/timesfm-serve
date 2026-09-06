import { readFileSync } from 'node:fs'
import type { IncomingMessage, ServerResponse } from 'node:http'
import type { Plugin } from 'vite'

// Local development adapter: only explicit read routes are forwarded; the key never enters the bundle.
export function weatherApiProxy(): Plugin {
  const origin = process.env.WEATHER_API_ORIGIN || 'https://88novucbtj.execute-api.us-east-1.amazonaws.com'
  const key = process.env.WEATHER_API_KEY_FILE ? readFileSync(process.env.WEATHER_API_KEY_FILE, 'utf8').trim() : ''
  const upstream = new URL(origin)
  if (upstream.protocol !== 'https:' && !(upstream.protocol === 'http:' && ['127.0.0.1', 'localhost'].includes(upstream.hostname))) {
    throw new Error('Weather API origin must use HTTPS or loopback HTTP')
  }
  function middleware(req: IncomingMessage, res: ServerResponse, next: () => void) {
    if (!req.url?.startsWith('/api/')) return next()
    res.setHeader('Content-Type', 'application/json')
    res.setHeader('Cache-Control', 'no-store')
    const reply = (status: number, data: unknown) => { res.statusCode = status; res.end(JSON.stringify(data)) }
    if (req.method !== 'GET') return reply(405, { detail: 'method_not_allowed' })
    if (req.url === '/api/connection') return reply(200, { configured: !!key, origin: upstream.origin })
    const path = req.url.slice(4)
    if (!/^\/v1\/weather\/stations(?:\/(42410099999|43128599999|43279099999)\/(latest|status))?$/.test(path)) {
      return reply(404, { detail: 'route_not_found' })
    }
    if (!key) return reply(503, { detail: 'api_not_configured' })
    fetch(new URL(path, upstream), { headers: { 'x-api-key': key, Accept: 'application/json' }, signal: AbortSignal.timeout(15_000), redirect: 'error' })
      .then(async response => {
        const body = await response.json()
        reply(response.status, body)
      }).catch(() => reply(502, { detail: 'upstream_unavailable' }))
  }
  return { name: 'weather-api-proxy', configureServer(server) { server.middlewares.use(middleware) },
    configurePreviewServer(server) { server.middlewares.use(middleware) } }
}
