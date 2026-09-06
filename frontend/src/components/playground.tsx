import { useEffect, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { ArrowDownToLine, ArrowUpRight, Braces, Eye, EyeOff, KeyRound, Play, RefreshCw, ShieldCheck, Terminal, TriangleAlert, X } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { CopyButton } from '@/components/copy-button'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Tabs, TabsList, TabsTab } from '@/components/ui/tabs'
import { requestExample } from '@/lib/account'
import { downloadJson, getJson } from '@/lib/weather'

const ENDPOINTS = [{ id: 'stations', label: 'List stations' }, { id: 'latest', label: 'Latest forecast' },
  { id: 'status', label: 'Station status' }, { id: 'replays', label: 'Replay catalog' }] as const
const STATIONS = [{ id: '42410099999', name: 'Guwahati' }, { id: '43128599999', name: 'Hyderabad' }, { id: '43279099999', name: 'Chennai' }]
const HEADERS = ['content-type', 'x-request-id', 'x-ratelimit-limit', 'x-ratelimit-remaining', 'x-ratelimit-reset', 'retry-after']
type Result = { status: number; statusText: string; elapsed: number; bytes: number; text: string; body: unknown; headers: [string, string][]; path: string }

export function Playground() {
  const connection = useQuery({ queryKey: ['playground-connection'], queryFn: ({ signal }) => getJson<{ origin: string }>('/api/playground/connection', signal) })
  const [endpoint, setEndpoint] = useState<string>('stations')
  const [station, setStation] = useState('42410099999')
  const [key, setKey] = useState('')
  const [visible, setVisible] = useState(false)
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState<Result | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [tab, setTab] = useState('body')
  const [language, setLanguage] = useState<'curl' | 'python'>('curl')
  const active = useRef<AbortController | null>(null)
  const stationEndpoint = endpoint === 'latest' || endpoint === 'status'
  const path = stationEndpoint ? `/v1/weather/stations/${station}/${endpoint}` : `/v1/weather/${endpoint}`
  const validKey = /^tfm_[A-Za-z0-9_-]{32}$/.test(key.trim())
  const example = requestExample(language, connection.data?.origin || '', path)
  useEffect(() => () => { active.current?.abort(); active.current = null }, [])
  useEffect(() => {
    if (connection.isPending || window.location.hash !== '#playground') return
    const frame = window.requestAnimationFrame(() => document.getElementById('playground')?.scrollIntoView({ block: 'start' }))
    return () => window.cancelAnimationFrame(frame)
  }, [connection.isPending])

  async function send() {
    if (!validKey || active.current || !connection.data) return
    const controller = new AbortController()
    active.current = controller
    setBusy(true); setError(null); setResult(null)
    const started = performance.now()
    try {
      const response = await fetch('/api/playground' + path, {
        headers: { 'x-api-key': key.trim(), Accept: 'application/json' }, credentials: 'omit', cache: 'no-store',
        signal: AbortSignal.any([controller.signal, AbortSignal.timeout(20_000)]),
      })
      const raw = await response.text()
      let body: unknown = raw
      try { body = JSON.parse(raw) } catch { /* Non-JSON upstream errors remain inspectable. */ }
      if (active.current !== controller) return
      setResult({ status: response.status, statusText: response.statusText, elapsed: Math.round(performance.now() - started),
        bytes: new TextEncoder().encode(raw).length, text: typeof body === 'string' ? raw : JSON.stringify(body, null, 2), body,
        headers: HEADERS.flatMap(name => response.headers.has(name) ? [[name, response.headers.get(name)!] as [string, string]] : []), path })
    } catch (error) {
      if (active.current === controller) setError(controller.signal.aborted ? 'Request cancelled.'
        : error instanceof DOMException && error.name === 'TimeoutError' ? 'The request timed out.' : 'Could not reach the API. Please try again.')
    } finally {
      if (active.current === controller) { active.current = null; setBusy(false) }
    }
  }
  return <section id="playground" className="playground" aria-labelledby="playground-title">
    <div className="section-heading playground-heading"><h2 id="playground-title"><Terminal size={17} />API playground</h2>
      <span className="playground-scope"><ShieldCheck size={14} />Read-only</span></div>
    <div className="playground-origin"><span>Base URL</span><code>{connection.data?.origin || 'Connecting...'}</code>
      {connection.data && <a href={connection.data.origin + '/docs'} target="_blank" rel="noreferrer">API reference<ArrowUpRight size={13} /></a>}</div>
    {connection.isError && <div className="account-message account-error" role="alert"><TriangleAlert size={16} />Playground connection unavailable<Button size="sm" variant="outline" onClick={() => void connection.refetch()}><RefreshCw />Retry</Button></div>}
    <div className="playground-workspace">
      <form className="playground-request" onSubmit={event => { event.preventDefault(); void send() }}>
        <h3>Request</h3>
        <label htmlFor="playground-endpoint">Endpoint</label>
        <Select value={endpoint} onValueChange={setEndpoint} disabled={busy}><SelectTrigger id="playground-endpoint" aria-label="Endpoint"><SelectValue /></SelectTrigger><SelectContent>
          {ENDPOINTS.map(item => <SelectItem key={item.id} value={item.id}>{item.label}</SelectItem>)}
        </SelectContent></Select>
        {stationEndpoint && <><label htmlFor="playground-station">Station</label><Select value={station} onValueChange={setStation} disabled={busy}><SelectTrigger id="playground-station" aria-label="Station"><SelectValue /></SelectTrigger><SelectContent>
          {STATIONS.map(item => <SelectItem key={item.id} value={item.id}>{item.name}</SelectItem>)}
        </SelectContent></Select></>}
        <div className="playground-auth-heading"><label htmlFor="playground-key">API key</label><a href="#api-account"><KeyRound size={12} />Manage keys</a></div>
        <div className="playground-key"><input id="playground-key" type={visible ? 'text' : 'password'} value={key} onChange={event => setKey(event.target.value)} placeholder="tfm_..." autoComplete="off" spellCheck={false} maxLength={128} disabled={busy} aria-describedby="key-privacy" />
          <Button type="button" size="icon" variant="ghost" title={visible ? 'Hide API key' : 'Show API key'} aria-label={visible ? 'Hide API key' : 'Show API key'} onClick={() => setVisible(!visible)}>{visible ? <EyeOff /> : <Eye />}</Button>
          {key && <Button type="button" size="icon" variant="ghost" title="Clear API key" aria-label="Clear API key" disabled={busy} onClick={() => { setKey(''); setVisible(false) }}><X /></Button>}</div>
        <p id="key-privacy" className="playground-privacy">Kept only while this page is open.</p>
        {key && !validKey && <p className="account-error">Enter a valid Forecast Lab API key.</p>}
        <div className="playground-send">{busy ? <Button type="button" variant="outline" onClick={() => active.current?.abort()}><X />Cancel request</Button>
          : <Button type="submit" disabled={!validKey || !connection.data}><Play />Send request</Button>}</div>
        <dl className="playground-request-headers"><dt>Request headers</dt><dd><span>Accept</span><code>application/json</code></dd><dd><span>x-api-key</span><code>{key ? '********' : 'Not set'}</code></dd></dl>
      </form>
      <section className="playground-response" aria-label="API response">
        <div className="playground-request-line"><span>GET</span><code>{result?.path || path}</code></div>
        <div className="playground-response-toolbar"><Tabs value={tab} onValueChange={value => setTab(value as string)}><TabsList variant="underline"><TabsTab value="body">Response</TabsTab><TabsTab value="headers">Headers</TabsTab></TabsList></Tabs>
          {result && <div><CopyButton value={tab === 'headers' ? result.headers.map(([key, value]) => key + ': ' + value).join('\n') : result.text} label="Copy response" />
            <Button size="icon" variant="ghost" title="Download response" aria-label="Download response" onClick={() => downloadJson(result.body, 'weather-response.json')}><ArrowDownToLine /></Button>
            <Button size="icon" variant="ghost" title="Clear response" aria-label="Clear response" onClick={() => setResult(null)}><X /></Button></div>}</div>
        {busy ? <div className="playground-empty" role="status"><RefreshCw size={22} className="spin" /><span>Waiting for the API...</span></div> : error ?
          <div className="playground-empty" role="status"><TriangleAlert size={23} /><span>{error}</span></div> : !result ?
            <div className="playground-empty"><Braces size={28} /><span>No request sent</span></div> : <>
              <div className="playground-response-meta" role="status"><span className={result.status < 400 ? 'response-ok' : 'response-error'}>{result.status} {result.statusText}</span><span>{result.elapsed.toLocaleString()} ms</span><span>{result.bytes.toLocaleString()} bytes</span>
                {result.headers.some(([name]) => name === 'x-ratelimit-remaining') && <span>{result.headers.find(([name]) => name === 'x-ratelimit-remaining')?.[1]} requests left</span>}</div>
              {tab === 'body' ? <pre className="playground-response-body" tabIndex={0}><code>{result.text}</code></pre> :
                <dl className="playground-headers">{result.headers.map(([name, value]) => <div key={name}><dt>{name}</dt><dd>{value}</dd></div>)}</dl>}
            </>}
      </section>
    </div>
    {connection.data && <section className="playground-example"><div className="section-heading"><h3>Request code</h3><div className="playground-code-actions"><div className="segmented" role="group" aria-label="Code language">
      <button aria-pressed={language === 'curl'} onClick={() => setLanguage('curl')}>cURL</button><button aria-pressed={language === 'python'} onClick={() => setLanguage('python')}>Python</button></div><CopyButton value={example} label="Copy request code" /></div></div>
      <pre className="request-code"><code>{example}</code></pre></section>}
    <dl className="api-contract"><div><dt>Authentication</dt><dd><code>x-api-key</code></dd></div><div><dt>Temperature</dt><dd>Celsius</dd></div><div><dt>Freshness</dt><dd>503 when unavailable or stale</dd></div></dl>
  </section>
}
