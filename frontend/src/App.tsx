import { useEffect, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link, Navigate, Route, Routes, useLocation, useSearchParams } from 'react-router'
import { motion, useReducedMotion } from 'motion/react'
import { Button } from '@/components/ui/button'
import { Tabs, TabsList, TabsTab } from '@/components/ui/tabs'
import { ForecastPanel } from '@/components/forecast-panel'
import { Benchmarks } from '@/components/benchmarks'
import { RunArchive } from '@/components/run-archive'
import { ApiAccess, GithubLogo } from '@/components/api-access'
import * as Icon from '@/components/icons'
import { useSession } from '@/lib/account'
import { forecastHref, legacyDestination, PAGE_PATHS, SECTION, type Page } from '@/lib/navigation'
import { PricePanel } from '@/components/price-panel'
import { PriceScorecard } from '@/components/price-scorecard'
import type { PriceForecast, Scorecard } from '@/lib/prices'
import { adaptLive, ApiError, dateLabel, getJson, timeLabel, type Catalog, type Forecast, type LiveStatus, type Mode, type Zone } from '@/lib/weather'

// Storyboard: navigation changes fade the new view in over 180ms; plotted values never animate.
const VIEW_MOTION = { offset: 4, duration: 0.18, ease: [0.2, 0, 0, 1] as const }
const NAV = [{ id: 'forecasts', label: 'Forecasts', icon: Icon.ChartLine }, { id: 'benchmarks', label: 'Benchmarks', icon: Icon.Flask },
  { id: 'runs', label: 'Run archive', icon: Icon.Clock }] as const
const PRICE_NAV = [{ id: 'prices', label: 'Day ahead', icon: Icon.Bolt },
  { id: 'scorecard', label: 'Track record', icon: Icon.Gauge }] as const
const LABELS: Record<Page, string> = { forecasts: 'Forecasts', benchmarks: 'Benchmarks', runs: 'Run archive',
  prices: 'Day ahead', scorecard: 'Track record', access: 'API keys' }
const SECTION_LABEL = { weather: 'Weather', power: 'Power prices', developer: 'Developer' } as const
const STATION_TINTS = ['#386aff', '#ff6802', '#16a34a']
const DOCS_URL = 'https://88novucbtj.execute-api.us-east-1.amazonaws.com/docs'

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
  const priceDay = params.get('day') || ''
  const priceQuery = useQuery({
    queryKey: ['price', priceDay || 'latest'], enabled: view === 'prices',
    queryFn: ({ signal }) => getJson<PriceForecast>('/api/v1/iex/forecast/' + (priceDay || 'latest'), signal),
  })
  const scorecardQuery = useQuery({
    queryKey: ['scorecard'], enabled: view === 'scorecard',
    queryFn: ({ signal }) => getJson<Scorecard>('/api/v1/iex/scorecard?days=90', signal),
  })
  const label = view === 'not-found' ? 'Page not found' : LABELS[view]
  useEffect(() => {
    window.document.title = `${label} | Forecast Lab`
    const target = hash === '#playground' ? 'playground' : hash === '#api-account' ? 'api-account' : 'main'
    const frame = window.requestAnimationFrame(() => {
      window.document.getElementById('main')?.focus({ preventScroll: true })
      if (target === 'main') window.scrollTo(0, 0)
      else window.document.getElementById(target)?.scrollIntoView({ block: 'start' })
    })
    return () => window.cancelAnimationFrame(frame)
  }, [label, hash])
  function filter(name: string, value?: string) {
    const next = new URLSearchParams(params)
    if (value) next.set(name, value); else next.delete(name)
    setParams(next)
  }
  const error = forecast.error
  const document = !error && forecast.data && (!live || forecast.data.id !== expiredId) ? forecast.data : undefined
  const user = session.data?.user
  const section = view === 'not-found' ? 'weather' : SECTION[view]
  const modeTabs = <Tabs value={mode} onValueChange={value => filter('mode', value === 'experimental_live' ? 'live' : undefined)} className="mode-tabs">
    <TabsList><TabsTab value="historical_replay"><Icon.Database size={13} />Historical</TabsTab><TabsTab value="experimental_live"><Icon.Signal size={13} />Live</TabsTab></TabsList>
  </Tabs>

  return <div className="app-frame">
    <a className="skip-link" href="#main">Skip to content</a>
    <aside className="sidebar">
      <Link to="/prices" className="brand" aria-label="Forecast Lab home"><span className="brand-mark"><Icon.Layers size={14} /></span><span>Forecast Lab</span></Link>
      <nav className="nav-group" aria-label="Power prices"><span className="nav-label">Power prices</span>
        {PRICE_NAV.map(item => <Link key={item.id} to={PAGE_PATHS[item.id]} aria-current={view === item.id ? 'page' : undefined}>
          <item.icon /><span>{item.label}</span></Link>)}
      </nav>
      <nav className="nav-group" aria-label="Weather"><span className="nav-label">Weather</span>
        {NAV.map(item => <Link key={item.id} to={PAGE_PATHS[item.id]} aria-current={view === item.id ? 'page' : undefined}>
          <item.icon /><span>{item.label}</span></Link>)}
        {catalog && <div className="station-nav">
          {catalog.stations.map((s, i) => <Link key={s.id} to={forecastHref(s.id, zone, mode)} aria-current={view === 'forecasts' && station?.id === s.id ? 'page' : undefined}>
            <span className="station-tile" style={{ background: STATION_TINTS[i % STATION_TINTS.length] }}>{s.name[0]}</span><span>{s.name}</span><small>{s.icao}</small>
          </Link>)}
        </div>}
      </nav>
      <div className="nav-group"><span className="nav-label">Developer</span>
        <Link to="/api-keys" aria-current={view === 'access' && hash !== '#playground' ? 'page' : undefined}><Icon.Key /><span>API keys</span></Link>
        <Link to="/api-keys#playground" aria-current={view === 'access' && hash === '#playground' ? 'page' : undefined}><Icon.Terminal /><span>Playground</span></Link>
        <a href={DOCS_URL} target="_blank" rel="noreferrer"><Icon.Book /><span>API reference</span><Icon.ArrowUpRight size={12} className="nav-external" /></a>
        <a href="https://github.com/Gmin2/timesfm-serve" target="_blank" rel="noreferrer"><Icon.Github /><span>Source</span><Icon.ArrowUpRight size={12} className="nav-external" /></a>
      </div>
      <div className="sidebar-foot">
        <div className="model-card"><Icon.Satellite /><div><strong>TimesFM 3.0</strong>
          <small>{section === 'power' ? 'Zero-shot on IEX and grid data' : 'Zero-shot on ECMWF IFS'}</small></div></div>
        <Link to="/api-keys" className="account-row"><GithubLogo /><span>{user ? '@' + user.login : 'Sign in with GitHub'}</span><Icon.ChevronRight size={12} /></Link>
      </div>
    </aside>
    <div className="main-shell">
      <header className="topbar">
        <div className="crumbs">{section === 'power' ? <Icon.Bolt size={15} />
          : section === 'developer' ? <Icon.Key size={15} /> : <Icon.Cloud size={15} />}
          <span>{SECTION_LABEL[section]}</span><span className="crumb-sep">/</span><strong>{label}</strong></div>
        <div className="topbar-actions">
          {section === 'weather' && <div className="segmented" role="group" aria-label="Display timezone">{(['Asia/Kolkata', 'UTC'] as Zone[]).map(z =>
            <button key={z} aria-pressed={zone === z} onClick={() => filter('zone', z === 'UTC' ? 'UTC' : undefined)}>{z === 'UTC' ? 'UTC' : 'IST'}</button>)}</div>}
          <Button className="topbar-account" size="sm" variant="outline" asChild><Link to="/api-keys"><GithubLogo /><span>{user ? '@' + user.login : 'Sign in'}</span></Link></Button>
        </div>
      </header>
      <main id="main" className={'main-content' + (view === 'runs' ? ' flush' : '')} tabIndex={-1}>
        {view === 'not-found' ? <div className="empty-state"><h1>Page not found</h1><Button variant="outline" asChild><Link to="/forecasts">Back to forecasts</Link></Button></div> :
          view === 'access' ? <ApiAccess loginError={params.get('auth_error')} /> :
          view === 'prices' || view === 'scorecard' ? <motion.div key={view}
            initial={{ opacity: reducedMotion ? 1 : 0, y: reducedMotion ? 0 : VIEW_MOTION.offset }}
            animate={{ opacity: 1, y: 0 }} transition={{ duration: reducedMotion ? 0 : VIEW_MOTION.duration, ease: VIEW_MOTION.ease }}>
            <div className="page-head">
              <span className="page-tile"><Icon.Bolt size={18} /></span>
              <div className="page-title"><h1>{view === 'prices' ? 'Indian Energy Exchange' : 'Track record'}</h1>
                <span className="badge">DAM</span></div>
              <p>{view === 'prices'
                ? 'Day-ahead market clearing price, 96 blocks of 15 minutes, forecast before bidding opens at 10:00 IST.'
                : 'Every forecast scored against what the market actually cleared, once the day settled.'}</p>
              <div className="page-actions">
                <Button size="icon" variant="outline" title="Refresh" aria-label="Refresh"
                  disabled={priceQuery.isFetching || scorecardQuery.isFetching}
                  onClick={() => { void (view === 'prices' ? priceQuery.refetch() : scorecardQuery.refetch()) }}>
                  <Icon.Refresh className={priceQuery.isFetching || scorecardQuery.isFetching ? 'spin' : ''} /></Button>
              </div>
            </div>
            {view === 'prices'
              ? priceQuery.isPending ? <Loading />
                : priceQuery.data ? <PricePanel document={priceQuery.data} />
                : <State title="No forecast on record" detail="Nothing has been issued for this delivery day yet."
                    code={priceQuery.error instanceof ApiError ? priceQuery.error.detail : undefined}
                    onRetry={() => { void priceQuery.refetch() }} />
              : scorecardQuery.isPending ? <Loading />
                : scorecardQuery.data ? <PriceScorecard card={scorecardQuery.data} />
                : <State title="Could not load the track record" detail="The scorecard is unavailable."
                    onRetry={() => { void scorecardQuery.refetch() }} />}
          </motion.div> : catalogQuery.isError ? <State title="Could not load the experiment archive" detail="The saved dataset is unavailable." onRetry={() => catalogQuery.refetch()} /> :
          !catalog || !station ? <Loading /> :
          <motion.div key={view} initial={{ opacity: reducedMotion ? 1 : 0, y: reducedMotion ? 0 : VIEW_MOTION.offset }}
            animate={{ opacity: 1, y: 0 }} transition={{ duration: reducedMotion ? 0 : VIEW_MOTION.duration, ease: VIEW_MOTION.ease }}>
            {view === 'forecasts' ? <>
              <div className="page-head">
                <span className="page-tile"><Icon.Pin size={18} /></span>
                <div className="page-title"><h1>{station.name}</h1><span className="badge">{station.icao}</span></div>
                <p>{station.region}, India · {station.latitude.toFixed(4)}° N {station.longitude.toFixed(4)}° E · 48-hour temperature outlook</p>
                <div className="page-actions"><span className={'mode-status ' + (live ? 'live' : '')}><i />{live ? 'Experimental live' : 'Historical replay'}</span>
                  <Button size="icon" variant="outline" title="Refresh forecast" aria-label="Refresh forecast"
                    disabled={forecast.isFetching || (live && !connection.data?.configured)}
                    onClick={() => { void forecast.refetch(); if (live) void status.refetch() }}><Icon.Refresh className={forecast.isFetching ? 'spin' : ''} /></Button>
                </div>
              </div>
              {live && !connection.data?.configured ? <><div className="toolbar">{modeTabs}</div><State title="Live API is not connected" detail="The historical archive is available. No live forecast has been substituted." icon="server" /></> :
                forecast.isPending ? <><div className="toolbar">{modeTabs}</div><Loading /></> :
                !document ? <><div className="toolbar">{modeTabs}</div><State title={error instanceof ApiError && error.detail === 'live_forecast_stale' ? 'The latest forecast has expired' : live ? 'No current forecast available' : 'Could not load this forecast'}
                  detail={live ? 'The live feed has not published a valid forecast for this station.' : 'The saved case could not be loaded.'}
                  code={error instanceof ApiError ? error.detail : undefined} onRetry={() => { void forecast.refetch(); if (live) void status.refetch() }} /></> :
                  <ForecastPanel key={mode + station.id} lead={modeTabs} document={document} cases={cases} onCaseChange={id => filter('run', id)} zone={zone} />}
              {live && status.data && <LiveStatusPanel status={status.data} zone={zone} />}
              {!live && document && <div className="note"><Icon.Info /><p>Retrospective holdout from frozen inputs. Not an operational forecast or an India-wide model comparison.</p>
                <Link to="/benchmarks">View benchmarks<Icon.ArrowUpRight size={12} /></Link></div>}
            </> : view === 'benchmarks' ? <Benchmarks catalog={catalog} /> :
              <RunArchive catalog={catalog} station={station} zone={zone} />}
          </motion.div>}
      </main>
    </div>
  </div>
}

function Loading() { return <div className="loading-state" role="status" aria-label="Loading forecast"><div /><div /><div /><span>Loading forecast data…</span></div> }
function State({ title, detail, code, onRetry, icon }: { title: string; detail: string; code?: string; onRetry?: () => void; icon?: 'server' }) {
  return <div className="empty-state" role="status"><span className="empty-icon">{icon ? <Icon.Server size={20} /> : <Icon.Warning size={20} />}</span><h2>{title}</h2><p>{detail}</p>{code && <code>{code}</code>}
    {onRetry && <Button variant="outline" size="sm" onClick={onRetry}><Icon.Refresh />Retry</Button>}</div>
}
function LiveStatusPanel({ status, zone }: { status: LiveStatus; zone: Zone }) {
  return <section className="card live-pipeline"><h2><Icon.Signal />Latest ingestion</h2>
    <div><span>Source capture</span><b>{status.ingestion?.status.replaceAll('_', ' ') ?? 'Not recorded'}</b></div>
    <div><span>GPU job</span><b>{status.job?.status ?? 'Not queued'}</b></div>
    <div><span>Last checked</span><b>{dateLabel(status.checked_at, zone)} {timeLabel(status.checked_at, zone)}</b></div>
    {status.ingestion?.error_code && <code>{status.ingestion.error_code}</code>}
  </section>
}
