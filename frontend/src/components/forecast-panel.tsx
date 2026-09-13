import { useState, type ReactNode } from 'react'
import { Button } from '@/components/ui/button'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import * as Icon from './icons'
import { ForecastChart } from './forecast-chart'
import { dateLabel, downloadCsv, downloadJson, scorePoints, SERIES, timeLabel, type Case, type Forecast, type Series, type Zone } from '@/lib/weather'

export function ForecastPanel({ lead, document, cases, onCaseChange, zone }: {
  lead: ReactNode; document: Forecast; cases: Case[]; onCaseChange: (id: string) => void; zone: Zone
}) {
  const [visible, setVisible] = useState(new Set<Series>(['forecast', 'ecmwf', 'observed']))
  const [showRange, setShowRange] = useState(true)
  const [horizon, setHorizon] = useState(48)
  const [table, setTable] = useState(false)
  const isLive = document.mode === 'experimental_live'
  const points = document.points.slice(0, horizon)
  const selected = cases.findIndex(c => c.id === document.id)
  const score = scorePoints(points, 'forecast')
  const max = Math.max(...points.map(p => p.forecast))
  const min = Math.min(...points.map(p => p.forecast))
  const keys = (Object.keys(SERIES) as Series[]).filter(key => !isLive || (key !== 'ridge' && key !== 'observed'))
  const activeSeries = new Set([...visible].filter(key => keys.includes(key)))
  const zoneLabel = zone === 'UTC' ? 'UTC' : 'IST'
  function toggle(key: Series) {
    const next = new Set(visible)
    if (next.has(key)) { if (activeSeries.size === 1) return; next.delete(key) } else next.add(key)
    setVisible(next)
  }
  return <div className="forecast-panel">
    <div className="toolbar">
      {lead}
      <div className="run-picker">
        {!isLive ? <>
          <Select value={document.id} onValueChange={onCaseChange}>
            <SelectTrigger className="pill" aria-label="Forecast run"><Icon.Calendar size={14} /><SelectValue /></SelectTrigger>
            <SelectContent position="popper">{cases.map(c => <SelectItem value={c.id} key={c.id}>
              {dateLabel(c.origin, zone, { year: 'numeric' })} · {timeLabel(c.origin, zone)}
            </SelectItem>)}</SelectContent>
          </Select>
          <Button size="icon" variant="ghost" aria-label="Previous forecast run" title="Previous forecast run" disabled={selected >= cases.length - 1}
            onClick={() => onCaseChange(cases[selected + 1].id)}><Icon.ChevronLeft /></Button>
          <Button size="icon" variant="ghost" aria-label="Next forecast run" title="Next forecast run" disabled={selected <= 0}
            onClick={() => onCaseChange(cases[selected - 1].id)}><Icon.ChevronRight /></Button>
        </> : <span className="pill"><Icon.Calendar size={14} />{dateLabel(document.origin, zone, { year: 'numeric' })} · {timeLabel(document.origin, zone)}</span>}
      </div>
      <div className="toolbar-end">
        <div className="segmented" role="group" aria-label="Forecast horizon">
          {[24, 48].map(h => <button key={h} aria-pressed={horizon === h} onClick={() => setHorizon(h)}>{h}h</button>)}
        </div>
        <Button variant="outline" size="sm" aria-label="Export CSV" title="Export CSV" onClick={() => downloadCsv(points, document.id + '.csv')}><Icon.Download /><span>Export CSV</span></Button>
        <Button variant="outline" size="icon" onClick={() => downloadJson(document, document.id + '.json')} title="Download forecast JSON" aria-label="Download forecast JSON"><Icon.FileJson /></Button>
      </div>
    </div>
    {isLive && <div className="live-freshness"><span>Generated {dateLabel(document.generatedAt!, zone)} · {timeLabel(document.generatedAt!, zone)}</span>
      <span>Fresh until {dateLabel(document.freshUntil!, zone)} · {timeLabel(document.freshUntil!, zone)} {zoneLabel}</span></div>}
    <section className="card chart-card" aria-label="Temperature forecast">
      <div className="kpis">
        <Stat label="Forecast high" value={max.toFixed(1)} unit="°C" detail="TimesFM + ECMWF" />
        <Stat label="Forecast low" value={min.toFixed(1)} unit="°C" detail="TimesFM + ECMWF" />
        <Stat label="Mean absolute error" value={score?.mae_c.toFixed(2) ?? '—'} unit={score ? '°C' : ''}
          detail={score ? 'Selected run · ' + horizon + ' hours' : 'Awaiting verification'} />
        <Stat label="Observed coverage" value={score ? String(score.n) : '—'} unit={score ? '/ ' + horizon : ''}
          detail={score ? 'Matched NOAA hours' : 'Not yet available in API'} />
      </div>
      <div className="chart-head">
        <div className="series-controls">
          {keys.map(key => <label key={key} style={{ '--series-color': SERIES[key].color } as React.CSSProperties}>
            <input type="checkbox" checked={visible.has(key)} onChange={() => toggle(key)} disabled={activeSeries.size === 1 && visible.has(key)} />
            <span>{SERIES[key].label}</span>
          </label>)}
          <label className="range-control" style={{ '--series-color': SERIES.forecast.color } as React.CSSProperties}>
            <input type="checkbox" checked={showRange} onChange={e => setShowRange(e.target.checked)} /><span>p10–p90 range</span></label>
        </div>
        <div className="segmented icon-segment" role="group" aria-label="Data view">
          <button aria-label="Chart view" title="Chart view" aria-pressed={!table} onClick={() => setTable(false)}><Icon.ChartLine size={14} /></button>
          <button aria-label="Table view" title="Table view" aria-pressed={table} onClick={() => setTable(true)}><Icon.Rows size={14} /></button>
        </div>
      </div>
      {table ? <div className="hourly-table"><Table>
        <caption className="sr-only">Hourly temperatures in Celsius; missing observations are not filled.</caption>
        <TableHeader><TableRow><TableHead scope="col">Valid time ({zoneLabel})</TableHead>
          {[...activeSeries].map(key => <TableHead scope="col" key={key}>{SERIES[key].label} (°C)</TableHead>)}
          {showRange && <TableHead scope="col">p10–p90 (°C)</TableHead>}</TableRow></TableHeader>
        <TableBody>{points.map(p => <TableRow key={p.time}><TableCell>{dateLabel(p.time, zone)} {timeLabel(p.time, zone)}</TableCell>
          {[...activeSeries].map(key => <TableCell key={key}>{p[key]?.toFixed(2) ?? '—'}</TableCell>)}
          {showRange && <TableCell>{p.lower.toFixed(2)}–{p.upper.toFixed(2)}</TableCell>}</TableRow>)}</TableBody>
      </Table></div> : <ForecastChart points={points} zone={zone} visible={activeSeries} showRange={showRange} />}
      <div className="chart-footnote"><span><Icon.Info size={12} />2 m air temperature, hourly. Model quantiles are uncalibrated.</span>
        <span>{dateLabel(points[0].time, zone)} – {dateLabel(points[points.length - 1].time, zone)} · {horizon} forecast hours · {zoneLabel}</span></div>
    </section>
    <section className="spec-section"><h2>Run provenance</h2>
      <dl className="spec">
        <div><dt>Forecast origin</dt><dd>{dateLabel(document.origin, zone, { year: 'numeric' })} · {timeLabel(document.origin, zone)} {zoneLabel}</dd></div>
        <div><dt>ECMWF initialization</dt><dd>{dateLabel(document.guidanceRun, zone, { year: 'numeric' })} · {timeLabel(document.guidanceRun, zone)} {zoneLabel}</dd></div>
        <div><dt>Model</dt><dd>TimesFM 3.0 · p50 estimate</dd></div>
        <div><dt>Verification source</dt><dd>{isLive ? 'Pending observations' : 'NOAA GHCNh'}</dd></div>
        <div><dt>Case</dt><dd className="mono">{document.id}</dd></div>
        <div><dt>Model revision</dt><dd className="mono">{document.provenance.revision}</dd></div>
        <div><dt>Execution device</dt><dd className="mono">{document.provenance.device}</dd></div>
        {document.provenance.sha256 && <div><dt>Prediction SHA-256</dt><dd className="mono">{document.provenance.sha256}</dd></div>}
        {document.prospectiveFrom && <div><dt>Prospective targets begin</dt><dd className="mono">{document.prospectiveFrom}</dd></div>}
      </dl>
    </section>
  </div>
}

export function Stat({ label, value, unit, detail }: { label: string; value: string; unit?: string; detail: string }) {
  return <div className="stat"><span>{label}</span><div><strong>{value}</strong>{unit && <b>{unit}</b>}</div><small>{detail}</small></div>
}
