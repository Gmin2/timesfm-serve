import { useEffect, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link, Navigate, NavLink, Route, Routes, useLocation, useSearchParams } from 'react-router'
import { Activity, ArrowUpRight, ChartNoAxesCombined, Check, ChevronRight, CircleHelp, Clock3, CloudSun, Database, FlaskConical, GitBranch, KeyRound, MapPin, RefreshCw, Satellite, Server, TriangleAlert, Waves } from 'lucide-react'
import { motion, useReducedMotion } from 'motion/react'
import { Button } from '@/components/ui/button'
import { Tabs, TabsList, TabsTab } from '@/components/ui/tabs'
import { ForecastPanel } from '@/components/forecast-panel'
import { Benchmarks } from '@/components/benchmarks'
import { ApiAccess, GithubLogo } from '@/components/api-access'
import { useSession } from '@/lib/account'
import { forecastHref, legacyDestination, PAGE_PATHS, type Page } from '@/lib/navigation'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { adaptLive, ApiError, dateLabel, getJson, timeLabel, type Catalog, type Forecast, type LiveStatus, type Mode, type Station, type Zone } from '@/lib/weather'

// Storyboard: navigation changes fade the new view in over 180ms; plotted values never animate.
const VIEW_MOTION = { offset: 4, duration: 0.18, ease: [0.2, 0, 0, 1] as const }
const NAV = [{ id: 'forecasts', label: 'Forecasts', icon: ChartNoAxesCombined }, { id: 'benchmarks', label: 'Benchmarks', icon: FlaskConical }, { id: 'runs', label: 'Run archive', icon: Clock3 }, { id: 'access', label: 'API access', icon: KeyRound }] as const
export default function App() {
  return <Routes><Route path="/" element={<LegacyRedirect />} />
    {Object.entries(PAGE_PATHS).map(([page, path]) => <Route key={path} path={path} element={<Workspace view={page as Page} />} />)}
    <Route path="/playground" element={<Navigate to="/api-keys#playground" replace />} />
    <Route path="*" element={<Workspace view="not-found" />} />
  </Routes>
}

function LegacyRedirect() {
  const location = useLocation()
  return <Navigate to={legacyDestination(location.search)} replace />
}

function Workspace({ view }: { view: Page | 'not-found' }) {
  const session = useSession()
  const { hash } = useLocation()
  const [params, setParams] = useSearchParams()
  const stationId = params.get('station') || '42410099999'
  const caseId = params.get('run') || ''
  const mode: Mode = params.get('mode') === 'live' ? 'experimental_live' : 'historical_replay'
  const zone: Zone = params.get('zone') === 'UTC' ? 'UTC' : 'Asia/Kolkata'
  const [expiredId, setExpiredId] = useState<string>()
  const reducedMotion = useReducedMotion()
  const catalogQuery = useQuery({ queryKey: ['catalog'], queryFn: ({ signal }) => getJson<Catalog>('/data/catalog.json', signal), staleTime: Infinity })
  const connection = useQuery({ queryKey: ['connection'], queryFn: ({ signal }) => getJson<{ configured: boolean; origin: string }>('/api/connection', signal) })
  const catalog = catalogQuery.data
  const station = catalog?.stations.find(s => s.id === stationId) ?? catalog?.stations[0]
  const cases = catalog?.cases.filter(c => c.stationId === station?.id).sort((a, b) => b.origin.localeCompare(a.origin)) ?? []
  const selectedCase = cases.find(c => c.id === caseId) ?? cases[0]
  const live = mode === 'experimental_live'
  const liveEnabled = !!station && live && view === 'forecasts' && connection.data?.configured === true
  const status = useQuery({ queryKey: ['status', station?.id], enabled: liveEnabled,
    queryFn: ({ signal }) => getJson<LiveStatus>('/api/v1/weather/stations/' + station!.id + '/status', signal), refetchInterval: 60_000 })
  const forecast = useQuery({
    queryKey: ['forecast', mode, station?.id, live ? 'latest' : selectedCase?.id],
    enabled: view === 'forecasts' && (live ? liveEnabled : !!selectedCase),
    queryFn: async ({ signal }) => live
      ? adaptLive(await getJson<unknown>('/api/v1/weather/stations/' + station!.id + '/latest', signal), station!.id)
      : getJson<Forecast>('/data/cases/' + selectedCase!.id + '.json', signal),
    refetchInterval: live ? 60_000 : false,
  })
  useEffect(() => {
    if (!live || !forecast.data?.freshUntil) return
    const data = forecast.data
    const timer = window.setTimeout(() => setExpiredId(data.id), Math.max(0, Date.parse(data.freshUntil!) - Date.now()) + 1)
    return () => window.clearTimeout(timer)
  }, [live, forecast.data])
  useEffect(() => {
    window.document.title = `${NAV.find(page => page.id === view)?.label ?? 'Page not found'} | Forecast Lab`
    const target = hash === '#playground' ? 'playground' : hash === '#api-account' ? 'api-account' : 'main'
    const frame = window.requestAnimationFrame(() => {
      window.document.getElementById('main')?.focus({ preventScroll: true })
      if (target === 'main') window.scrollTo(0, 0)
      else window.document.getElementById(target)?.scrollIntoView({ block: 'start' })
    })
    return () => window.cancelAnimationFrame(frame)
  }, [view, hash])
  function filter(name: string, value?: string) {
    const next = new URLSearchParams(params)
    if (value) next.set(name, value); else next.delete(name)
    setParams(next)
  }
  const error = forecast.error
  const document = !error && forecast.data && (!live || forecast.data.id !== expiredId) ? forecast.data : undefined

  return <div className="app-shell">
    <a className="skip-link" href="#main">Skip to content</a>
    <aside className="sidebar">
      <Link to="/forecasts" className="brand" aria-label="Forecast Lab home"><span className="brand-mark"><Waves size={23} /></span><span>Forecast Lab<small>WEATHER RESEARCH</small></span></Link>
      <div className="workspace"><span className="workspace-icon"><CloudSun size={18} /></span><div>India station network<small>Temperature · 3 locations</small></div><span className="workspace-count">IN</span></div>
      <nav aria-label="Workspace"><span className="nav-label">Workspace</span>{NAV.map(item =>
        <NavLink key={item.id} to={PAGE_PATHS[item.id]} end>
          <item.icon size={17} /><span>{item.label}</span>{item.id === 'runs' && <small>99</small>}
        </NavLink>)}</nav>
      <div className="station-list"><span className="nav-label">Stations <span>03</span></span>
        {catalog?.stations.map(s => <Link key={s.id} to={forecastHref(s.id, zone, mode)} aria-current={view === 'forecasts' && station?.id === s.id ? 'page' : undefined}>
          <span className={'station-dot ' + (station?.id === s.id ? 'selected' : '')} /><span>{s.name}<small>{s.region}</small></span><b>{s.icao}</b>
        </Link>)}
      </div>
      <div className="sidebar-bottom"><div className="model-meta"><Satellite size={17} /><div>TimesFM 3.0<small>ECMWF post-processing</small></div></div>
        <a href="https://github.com/Gmin2/timesfm-serve" target="_blank" rel="noreferrer"><GitBranch size={15} />Source repository<ArrowUpRight size={14} /></a>
        <a href="https://88novucbtj.execute-api.us-east-1.amazonaws.com/docs" target="_blank" rel="noreferrer"><CircleHelp size={15} />API documentation<ArrowUpRight size={14} /></a>
        <div className="independent"><span />Independent research project</div>
      </div>
    </aside>
    <div className="main-shell">
      <header className="topbar"><div><span className="breadcrumb-icon"><CloudSun size={17} /></span><span>Weather</span><ChevronRight size={14} /><strong>{NAV.find(n => n.id === view)?.label ?? 'Page not found'}</strong></div>
        <div className="topbar-actions"><span className="region-label"><span className="region-dot" />India / 3 stations</span>
          <div className="segmented" role="group" aria-label="Display timezone">{(['Asia/Kolkata', 'UTC'] as Zone[]).map(z =>
            <button key={z} aria-pressed={zone === z} onClick={() => filter('zone', z === 'UTC' ? 'UTC' : undefined)}>{z === 'UTC' ? 'UTC' : 'IST'}</button>)}</div>
          <Button className="account-button" size="sm" variant="outline" asChild><Link to="/api-keys"><GithubLogo /><span>{session.data?.user ? '@' + session.data.user.login : 'Sign in'}</span></Link></Button></div>
      </header>
      <main id="main" className="main-content" tabIndex={-1}>
        {view === 'not-found' ? <div className="empty-state"><h1>Page not found</h1><Button variant="outline" asChild><Link to="/forecasts">Back to forecasts</Link></Button></div> :
          view === 'access' ? <ApiAccess loginError={params.get('auth_error')} /> : catalogQuery.isError ? <State title="Could not load the experiment archive" detail="The saved dataset is unavailable." onRetry={() => catalogQuery.refetch()} /> :
          !catalog || !station ? <Loading /> :
          <motion.div key={view} initial={{ opacity: reducedMotion ? 1 : 0, y: reducedMotion ? 0 : VIEW_MOTION.offset }}
            animate={{ opacity: 1, y: 0 }} transition={{ duration: reducedMotion ? 0 : VIEW_MOTION.duration, ease: VIEW_MOTION.ease }}>
            {view === 'forecasts' ? <>
              <div className="view-intro"><div><div className="eyebrow"><MapPin size={13} />{station.region}, India <span>/</span> {station.icao}</div><h1>{station.name}<span className="heading-secondary">Station forecast</span></h1>
                <p>{station.latitude.toFixed(4)}° N &nbsp; {station.longitude.toFixed(4)}° E <span className="dot-separator">·</span> 48-hour temperature outlook</p></div>
                <div className="intro-actions"><span className={'mode-status ' + (live ? 'live-status' : '')}><span />{live ? 'Experimental live' : 'Historical replay'}</span>
                  <Button size="icon" variant="outline" title="Refresh forecast" aria-label="Refresh forecast"
                    disabled={forecast.isFetching || (live && !connection.data?.configured)}
                    onClick={() => { void forecast.refetch(); if (live) void status.refetch() }}><RefreshCw className={forecast.isFetching ? 'spin' : ''} /></Button>
                </div>
              </div>
              <Tabs value={mode} onValueChange={value => filter('mode', value === 'experimental_live' ? 'live' : undefined)} className="mode-tabs">
                <TabsList variant="underline"><TabsTab value="historical_replay"><Database size={14} />Historical</TabsTab><TabsTab value="experimental_live"><Activity size={14} />Live forecasts</TabsTab></TabsList>
              </Tabs>
              {live && !connection.data?.configured ? <State title="Live API is not connected" detail="The historical archive is available. No live forecast has been substituted." icon="server" /> :
                forecast.isPending ? <Loading /> :
                !document ? <State title={error instanceof ApiError && error.detail === 'live_forecast_stale' ? 'The latest forecast has expired' : live ? 'No current forecast available' : 'Could not load this forecast'}
                  detail={live ? 'The live feed has not published a valid forecast for this station.' : 'The saved case could not be loaded.'}
                  code={error instanceof ApiError ? error.detail : undefined} onRetry={() => { void forecast.refetch(); if (live) void status.refetch() }} /> :
                  <ForecastPanel key={mode + station.id} document={document} cases={cases} onCaseChange={id => filter('run', id)} zone={zone} />}
              {live && status.data && <LiveStatusPanel status={status.data} zone={zone} />}
              {!live && document && <div className="research-note"><FlaskConical size={16} /><p>Retrospective holdout from frozen inputs. Not an operational forecast or an India-wide model comparison.</p>
                <Link to="/benchmarks">View benchmarks<ArrowUpRight size={14} /></Link></div>}
            </> : view === 'benchmarks' ? <Benchmarks catalog={catalog} /> :
              <RunArchive catalog={catalog} station={station} zone={zone} />}
          </motion.div>}
      </main>
      <footer className="app-footer"><span>Forecast Lab <span className="dot-separator">·</span> Independent prototype</span><span>NOAA observations + ECMWF guidance <span className="dot-separator">·</span> TimesFM 3.0</span></footer>
    </div>
  </div>
}

function Loading() { return <div className="loading-state" role="status" aria-label="Loading forecast"><div /><div /><div /><span>Loading forecast data…</span></div> }
function State({ title, detail, code, onRetry, icon }: { title: string; detail: string; code?: string; onRetry?: () => void; icon?: 'server' }) {
  return <div className="empty-state" role="status">{icon ? <Server size={28} /> : <TriangleAlert size={28} />}<h2>{title}</h2><p>{detail}</p>{code && <code>{code}</code>}
    {onRetry && <Button variant="outline" size="sm" onClick={onRetry}><RefreshCw />Retry</Button>}</div>
}
function LiveStatusPanel({ status, zone }: { status: LiveStatus; zone: Zone }) {
  return <section className="live-pipeline"><h2><Activity size={16} />Latest ingestion</h2>
    <div><span>Source capture</span><b>{status.ingestion?.status.replaceAll('_', ' ') ?? 'Not recorded'}</b></div>
    <div><span>GPU job</span><b>{status.job?.status ?? 'Not queued'}</b></div>
    <div><span>Last checked</span><b>{dateLabel(status.checked_at, zone)} {timeLabel(status.checked_at, zone)}</b></div>
    {status.ingestion?.error_code && <code>{status.ingestion.error_code}</code>}
  </section>
}
function RunArchive({ catalog, station, zone }: { catalog: Catalog; station: Station; zone: Zone }) {
  const [filter, setFilter] = useState('all')
  const rows = catalog.runs.filter(r => (filter === 'all' || r.station === station.id)).slice().sort((a, b) => b.origin.localeCompare(a.origin))
  return <section className="run-archive"><div className="view-intro"><div><h1>Run archive</h1><p>99 scheduled cases · Frozen multi-season holdout</p></div>
    <div className="segmented" role="group" aria-label="Run station filter"><button aria-pressed={filter === 'all'} onClick={() => setFilter('all')}>All stations</button><button aria-pressed={filter !== 'all'} onClick={() => setFilter('station')}>{station.name}</button></div></div>
    <Table><caption className="sr-only">All scheduled historical runs, including rejected cases.</caption><TableHeader><TableRow>
      {['Forecast origin', 'Station', 'Status', 'Observed hours', 'Result'].map(s => <TableHead scope="col" key={s}>{s}</TableHead>)}</TableRow></TableHeader>
      <TableBody>{rows.map(r => {
        const c = catalog.cases.find(c => c.stationId === r.station && Date.parse(c.origin) === Date.parse(r.origin))
        return <TableRow key={r.station + r.origin}><TableCell>{dateLabel(r.origin.replace(' ', 'T'), zone, { year: 'numeric' })} · {timeLabel(r.origin.replace(' ', 'T'), zone)}</TableCell>
          <TableCell>{catalog.stations.find(s => s.id === r.station)?.name}</TableCell>
          <TableCell><span className={'run-status ' + (c ? 'accepted' : 'rejected')}>{c ? <Check size={12} /> : <TriangleAlert size={12} />}{c ? 'Scored' : 'Data rejected'}</span></TableCell>
          <TableCell>{r.scored_hours || '—'}{r.scored_hours && ' / 48'}</TableCell>
          <TableCell>{c ? <Button variant="ghost" size="sm" asChild><Link to={forecastHref(r.station, zone, 'historical_replay', c.id)}>Open forecast<ArrowUpRight /></Link></Button> : <span className="muted">{r.reason.replaceAll('_', ' ')}</span>}</TableCell>
        </TableRow>
      })}</TableBody>
    </Table>
  </section>
}
