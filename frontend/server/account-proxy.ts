import type { IncomingMessage, ServerResponse } from 'node:http'
import type { Plugin } from 'vite'

// Cookie-authenticated routes never receive the local operator's weather API key.
export function accountProxy(): Plugin {
  const value = process.env.WEATHER_ACCOUNT_ORIGIN
  const upstream = value ? new URL(value) : null
  if (upstream && (upstream.username || upstream.password || upstream.pathname !== '/' || upstream.search || upstream.hash
    || (upstream.protocol !== 'https:' && !(upstream.protocol === 'http:' && ['127.0.0.1', 'localhost'].includes(upstream.hostname))))) {
    throw new Error('Account origin must be an HTTPS or loopback HTTP origin')
  }
  async function middleware(req: IncomingMessage, res: ServerResponse, next: () => void) {
    if (!req.url?.startsWith('/api/auth/') && !req.url?.startsWith('/api/v1/account/')) return next()
    const url = new URL(req.url, 'http://localhost')
    const path = url.pathname.slice(4)
    const allowed = path === '/auth/session' || path === '/auth/github' || path === '/auth/github/callback' ? ['GET']
      : path === '/auth/logout' ? ['POST'] : path === '/v1/account/keys' ? ['GET', 'POST']
        : /^\/v1\/account\/keys\/\d+$/.test(path) ? ['DELETE'] : []
    res.setHeader('Cache-Control', 'no-store')
    res.setHeader('Referrer-Policy', 'no-referrer')
    const reply = (status: number, body: unknown) => {
      res.statusCode = status; res.setHeader('Content-Type', 'application/json'); res.end(JSON.stringify(body))
    }
    if (!allowed.length) return reply(404, { detail: 'route_not_found' })
    if (!allowed.includes(req.method || '')) { res.setHeader('Allow', allowed.join(', ')); return reply(405, { detail: 'method_not_allowed' }) }
    if (!upstream) return path === '/auth/session'
      ? reply(200, { configured: false, user: null, csrf_token: null, api_origin: null, max_active_keys: 3 })
      : reply(503, { detail: 'account_api_not_configured' })
    try {
      const chunks: Buffer[] = []
      let length = 0
      for await (const chunk of req) {
        const buffer = Buffer.from(chunk); length += buffer.length
        if (length > 8192) return reply(413, { detail: 'request_too_large' })
        chunks.push(buffer)
      }
      const headers: Record<string, string> = { Accept: 'application/json' }
      for (const name of ['cookie', 'origin', 'x-csrf-token', 'content-type']) {
        const value = req.headers[name]
        if (typeof value === 'string') headers[name] = value
      }
      const response = await fetch(new URL(path + url.search, upstream), {
        method: req.method, headers, body: req.method === 'POST' ? Buffer.concat(chunks) : undefined,
        redirect: 'manual', signal: AbortSignal.timeout(15_000),
      })
      for (const name of ['content-type', 'location', 'retry-after']) {
        const value = response.headers.get(name)
        if (value) res.setHeader(name, value)
      }
      const cookies = response.headers.getSetCookie()
      if (cookies.length) res.setHeader('Set-Cookie', cookies)
      res.statusCode = response.status
      res.end(Buffer.from(await response.arrayBuffer()))
    } catch { reply(502, { detail: 'account_api_unavailable' }) }
  }
  return { name: 'account-proxy', configureServer(server) { server.middlewares.use(middleware) },
    configurePreviewServer(server) { server.middlewares.use(middleware) } }
}
