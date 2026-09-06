import type { IncomingMessage, ServerResponse } from 'node:http'
import type { Plugin } from 'vite'

const READ_PATH = /^\/v1\/weather\/(?:stations(?:\/(?:42410099999|43128599999|43279099999)\/(?:latest|status))?|replays)$/
const RESPONSE_HEADERS = ['content-type', 'x-request-id', 'x-ratelimit-limit', 'x-ratelimit-remaining', 'x-ratelimit-reset', 'retry-after']
const MAX_RESPONSE_BYTES = 1024 * 1024

export function playgroundProxy(): Plugin {
  const upstream = new URL(process.env.WEATHER_PLAYGROUND_ORIGIN || process.env.WEATHER_API_ORIGIN
    || 'https://88novucbtj.execute-api.us-east-1.amazonaws.com')
  if (upstream.username || upstream.password || upstream.pathname !== '/' || upstream.search || upstream.hash
    || (upstream.protocol !== 'https:' && !(upstream.protocol === 'http:' && ['127.0.0.1', 'localhost'].includes(upstream.hostname)))) {
    throw new Error('Playground origin must be HTTPS or loopback HTTP without a path')
  }
  async function middleware(req: IncomingMessage, res: ServerResponse, next: () => void) {
    if (!req.url?.startsWith('/api/playground/')) return next()
    res.setHeader('Cache-Control', 'no-store')
    const reply = (status: number, detail: string) => {
      res.statusCode = status; res.setHeader('Content-Type', 'application/json'); res.end(JSON.stringify({ detail }))
    }
    if (req.method !== 'GET') return reply(405, 'read_only_playground')
    if (req.url === '/api/playground/connection') {
      res.setHeader('Content-Type', 'application/json'); res.end(JSON.stringify({ origin: upstream.origin })); return
    }
    const path = req.url.slice('/api/playground'.length)
    if (!READ_PATH.test(path)) return reply(404, 'route_not_found')
    const key = req.headers['x-api-key']
    if (typeof key !== 'string' || !/^tfm_[A-Za-z0-9_-]{32}$/.test(key)) return reply(401, 'invalid_api_key')
    const controller = new AbortController()
    const disconnected = () => { if (!res.writableEnded) controller.abort() }
    res.on('close', disconnected)
    try {
      // Only the explicitly entered customer key is forwarded, never cookies or an operator key.
      const response = await fetch(new URL(path, upstream), {
        headers: { 'x-api-key': key, Accept: 'application/json' }, redirect: 'error',
        signal: AbortSignal.any([controller.signal, AbortSignal.timeout(15_000)]),
      })
      const chunks: Uint8Array[] = []
      const reader = response.body?.getReader()
      let size = 0
      if (reader) {
        while (true) {
          const { done, value } = await reader.read()
          if (done) break
          size += value.byteLength
          if (size > MAX_RESPONSE_BYTES) { await reader.cancel(); return reply(502, 'response_too_large') }
          chunks.push(value)
        }
      }
      for (const header of RESPONSE_HEADERS) {
        const value = response.headers.get(header)
        if (value) res.setHeader(header, value)
      }
      res.statusCode = response.status
      res.end(Buffer.concat(chunks, size))
    } catch {
      if (!res.destroyed) reply(502, 'playground_upstream_unavailable')
    } finally { res.off('close', disconnected) }
  }
  return { name: 'playground-proxy', configureServer(server) { server.middlewares.use(middleware) },
    configurePreviewServer(server) { server.middlewares.use(middleware) } }
}
