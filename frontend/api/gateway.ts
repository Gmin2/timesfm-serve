import { Buffer } from 'node:buffer'
import process from 'node:process'

const DEFAULT_API_ORIGIN = 'https://88novucbtj.execute-api.us-east-1.amazonaws.com'
const WEATHER_PATH = /^\/v1\/weather\/stations(?:\/(?:42410099999|43128599999|43279099999)\/(?:latest|status))?$/
const PLAYGROUND_PATH = /^\/v1\/weather\/(?:stations(?:\/(?:42410099999|43128599999|43279099999)\/(?:latest|status))?|replays)$/
const RESPONSE_HEADERS = ['content-type', 'x-request-id', 'x-ratelimit-limit', 'x-ratelimit-remaining', 'x-ratelimit-reset', 'retry-after']
const PRIVATE_HEADERS = { 'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer', 'X-Content-Type-Options': 'nosniff' }

function origin(value: string): string {
  const url = new URL(value)
  if (url.protocol !== 'https:' || url.username || url.password || url.pathname !== '/' || url.search || url.hash) {
    throw new Error('Production API origin must be an HTTPS origin without a path')
  }
  return url.origin
}

function json(status: number, body: unknown, extra: Record<string, string> = {}): Response {
  return Response.json(body, { status, headers: { ...PRIVATE_HEADERS, ...extra } })
}

async function limitedBody(stream: ReadableStream<Uint8Array> | null, limit: number): Promise<Buffer<ArrayBuffer>> {
  const reader = stream?.getReader()
  const chunks: Uint8Array[] = []
  let size = 0
  if (reader) {
    try {
      while (true) {
        const { done, value } = await reader.read()
        if (done) break
        size += value.byteLength
        if (size > limit) {
          await reader.cancel()
          throw new RangeError('Body exceeds size limit')
        }
        chunks.push(value)
      }
    } finally { reader.releaseLock() }
  }
  return Buffer.concat(chunks, size)
}

async function forward(request: Request, target: URL, account = false, key?: string): Promise<Response> {
  const headers = new Headers({ Accept: 'application/json' })
  let body: Buffer<ArrayBuffer> | undefined
  if (account) {
    for (const name of ['cookie', 'origin', 'x-csrf-token', 'content-type']) {
      const value = request.headers.get(name)
      if (value) headers.set(name, value)
    }
    if (request.method === 'POST') {
      try { body = await limitedBody(request.body, 8192) }
      catch { return json(413, { detail: 'request_too_large' }) }
    }
  } else if (key) headers.set('x-api-key', key)

  try {
    const upstream = await fetch(target, {
      method: request.method, headers, body,
      redirect: account ? 'manual' : 'error',
      signal: AbortSignal.any([request.signal, AbortSignal.timeout(15_000)]),
    })
    const resultHeaders = new Headers(PRIVATE_HEADERS)
    for (const name of RESPONSE_HEADERS) {
      const value = upstream.headers.get(name)
      if (value) resultHeaders.set(name, value)
    }
    // Only account routes may return session cookies or OAuth redirects.
    if (account) {
      const location = upstream.headers.get('location')
      if (location) resultHeaders.set('location', location)
      for (const cookie of upstream.headers.getSetCookie()) resultHeaders.append('set-cookie', cookie)
    }
    const content = await limitedBody(upstream.body, 1024 * 1024)
    return new Response([204, 205, 304].includes(upstream.status) ? null : content, {
      status: upstream.status, headers: resultHeaders,
    })
  } catch {
    return json(502, { detail: account ? 'account_api_unavailable' : 'upstream_unavailable' })
  }
}

async function gateway(request: Request): Promise<Response> {
  const url = new URL(request.url)
  const path = '/' + (url.searchParams.get('__path') || '')
  url.searchParams.delete('__path')
  const api = origin(process.env.WEATHER_API_ORIGIN || DEFAULT_API_ORIGIN)

  if (path.startsWith('/auth/') || path.startsWith('/v1/account/')) {
    const allowed = ['/auth/session', '/auth/github', '/auth/github/callback'].includes(path) ? ['GET']
      : path === '/auth/logout' ? ['POST'] : path === '/v1/account/keys' ? ['GET', 'POST']
        : /^\/v1\/account\/keys\/\d+$/.test(path) ? ['DELETE'] : []
    if (!allowed.length) return json(404, { detail: 'route_not_found' })
    if (!allowed.includes(request.method)) return json(405, { detail: 'method_not_allowed' }, { Allow: allowed.join(', ') })
    const account = process.env.WEATHER_ACCOUNT_ORIGIN
    if (!account) return path === '/auth/session'
      ? json(200, { configured: false, user: null, csrf_token: null, api_origin: api, max_active_keys: 3 })
      : json(503, { detail: 'account_api_not_configured' })
    return forward(request, new URL(path + url.search, origin(account)), true)
  }

  if (request.method !== 'GET') return json(405, { detail: 'method_not_allowed' }, { Allow: 'GET' })
  if (path.startsWith('/playground/')) {
    const playground = origin(process.env.WEATHER_PLAYGROUND_ORIGIN || api)
    if (path === '/playground/connection' && !url.search) return json(200, { origin: playground })
    const route = path.slice('/playground'.length)
    if (!PLAYGROUND_PATH.test(route) || url.search) return json(404, { detail: 'route_not_found' })
    const key = request.headers.get('x-api-key')
    if (!key || !/^tfm_[A-Za-z0-9_-]{32}$/.test(key)) return json(401, { detail: 'invalid_api_key' })
    return forward(request, new URL(route, playground), false, key)
  }

  // Never use the operator's replay-capable key for a public dashboard.
  const dashboardKey = process.env.WEATHER_DASHBOARD_API_KEY
  if (path === '/connection' && !url.search) return json(200, { configured: !!dashboardKey, origin: api })
  if (!WEATHER_PATH.test(path) || url.search) return json(404, { detail: 'route_not_found' })
  if (!dashboardKey) return json(503, { detail: 'api_not_configured' })
  return forward(request, new URL(path, api), false, dashboardKey)
}

export default {
  async fetch(request: Request): Promise<Response> {
    try { return await gateway(request) }
    catch { return json(503, { detail: 'gateway_not_configured' }) }
  },
}
