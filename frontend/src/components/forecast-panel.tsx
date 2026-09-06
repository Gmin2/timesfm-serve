import { useState } from 'react'
import { ArrowDownToLine, CalendarDays, ChevronLeft, ChevronRight, FileJson, Info, Thermometer, Table2, ChartNoAxesCombined } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { ForecastChart } from './forecast-chart'
import { dateLabel, downloadCsv, downloadJson, scorePoints, SERIES, timeLabel, type Case, type Forecast, type Series, type Zone } from '@/lib/weather'

export function ForecastPanel({ document, cases, onCaseChange, zone }: {
  document: Forecast; cases: Case[]; onCaseChange: (id: string) => void; zone: Zone
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
  function toggle(key: Series) {
    const next = new Set(visible)
    if (next.has(key)) { if (activeSeries.size === 1) return; next.delete(key) } else next.add(key)
    setVisible(next)
  }
  return <div className="forecast-panel">
    <div className="forecast-controls">
      <div className="run-picker">
        <CalendarDays size={16} />
        {!isLive ? <>
          <Select value={document.id} onValueChange={onCaseChange}>
            <SelectTrigger aria-label="Forecast run"><SelectValue /></SelectTrigger>
            <SelectContent position="popper">{cases.map(c => <SelectItem value={c.id} key={c.id}>
              {dateLabel(c.origin, zone, { year: 'numeric' })} · {timeLabel(c.origin, zone)}
            </SelectItem>)}</SelectContent>
          </Select>
          <Button size="icon" variant="ghost" aria-label="Previous forecast run" title="Previous forecast run" disabled={selected >= cases.length - 1}
            onClick={() => onCaseChange(cases[selected + 1].id)}><ChevronLeft /></Button>
          <Button size="icon" variant="ghost" aria-label="Next forecast run" title="Next forecast run" disabled={selected <= 0}
            onClick={() => onCaseChange(cases[selected - 1].id)}><ChevronRight /></Button>
        </> : <span>{dateLabel(document.origin, zone, { year: 'numeric' })} · {timeLabel(document.origin, zone)}</span>}
      </div>
      <div className="download-controls">
        <span className="tag">{isLive ? 'Experimental live' : 'Saved holdout'}</span>
        <Button variant="outline" size="sm" aria-label="Export CSV" title="Export CSV" onClick={() => downloadCsv(points, document.id + '.csv')}><ArrowDownToLine /><span>Export CSV</span></Button>
        <Button variant="ghost" size="icon" onClick={() => downloadJson(document, document.id + '.json')} title="Download forecast JSON" aria-label="Download forecast JSON"><FileJson /></Button>
      </div>
    </div>
    {isLive && <div className="live-freshness"><span>Generated {dateLabel(document.generatedAt!, zone)} · {timeLabel(document.generatedAt!, zone)}</span>
      <span>Fresh until {dateLabel(document.freshUntil!, zone)} · {timeLabel(document.freshUntil!, zone)} {zone === 'UTC' ? 'UTC' : 'IST'}</span></div>}
    <div className="forecast-stats">
      <Stat label="Forecast high" value={max.toFixed(1)} unit="°C" detail="TimesFM + ECMWF" />
      <Stat label="Forecast low" value={min.toFixed(1)} unit="°C" detail="TimesFM + ECMWF" />
      <Stat label="Mean absolute error" value={score?.mae_c.toFixed(2) ?? '—'} unit={score ? '°C' : ''}
        detail={score ? 'Selected run · ' + horizon + ' hours' : 'Awaiting verification'} />
      <Stat label="Observed coverage" value={score ? String(score.n) : '—'} unit={score ? '/ ' + horizon : ''}
        detail={score ? 'Matched NOAA hours' : 'Not yet available in API'} />
    </div>
    <section className="chart-section" aria-label="Temperature forecast">
      <div className="section-heading">
        <div><h2><Thermometer size={17} />Temperature forecast</h2><p>2 m air temperature · Hourly · {zone === 'UTC' ? 'UTC' : 'India Standard Time'}</p></div>
        <div className="chart-actions">
          <div className="segmented" role="group" aria-label="Forecast horizon">
            {[24, 48].map(h => <button key={h} aria-pressed={horizon === h} onClick={() => setHorizon(h)}>{h}h</button>)}
          </div>
          <div className="segmented icon-segment" role="group" aria-label="Data view">
            <button aria-label="Chart view" title="Chart view" aria-pressed={!table} onClick={() => setTable(false)}><ChartNoAxesCombined size={16} /></button>
            <button aria-label="Table view" title="Table view" aria-pressed={table} onClick={() => setTable(true)}><Table2 size={16} /></button>
          </div>
        </div>
      </div>
      <div className="series-controls">
        {keys.map(key => <label key={key} style={{ '--series-color': SERIES[key].color } as React.CSSProperties}>
          <input type="checkbox" checked={visible.has(key)} onChange={() => toggle(key)} disabled={activeSeries.size === 1 && visible.has(key)} />
          <span>{SERIES[key].label}</span>
        </label>)}
        <label className="range-control"><input type="checkbox" checked={showRange} onChange={e => setShowRange(e.target.checked)} />p10–p90 range</label>
      </div>
      {table ? <div className="hourly-table"><Table>
        <caption className="sr-only">Hourly temperatures in Celsius; missing observations are not filled.</caption>
        <TableHeader><TableRow><TableHead scope="col">Valid time ({zone === 'UTC' ? 'UTC' : 'IST'})</TableHead>
          {[...activeSeries].map(key => <TableHead scope="col" key={key}>{SERIES[key].label} (°C)</TableHead>)}
          {showRange && <TableHead scope="col">p10–p90 (°C)</TableHead>}</TableRow></TableHeader>
        <TableBody>{points.map(p => <TableRow key={p.time}><TableCell>{dateLabel(p.time, zone)} {timeLabel(p.time, zone)}</TableCell>
          {[...activeSeries].map(key => <TableCell key={key}>{p[key]?.toFixed(2) ?? '—'}</TableCell>)}
          {showRange && <TableCell>{p.lower.toFixed(2)}–{p.upper.toFixed(2)}</TableCell>}</TableRow>)}</TableBody>
      </Table></div> : <ForecastChart points={points} zone={zone} visible={activeSeries} showRange={showRange} />}
      <div className="chart-footnote"><span><Info size={13} />Model quantiles are uncalibrated.</span>
        <span>{dateLabel(points[0].time, zone)} – {dateLabel(points[points.length - 1].time, zone)} · {horizon} forecast hours</span></div>
    </section>
    <div className="provenance-strip">
      <div><span>Forecast origin</span><b>{dateLabel(document.origin, zone)} · {timeLabel(document.origin, zone)}</b></div>
      <div><span>ECMWF initialization</span><b>{dateLabel(document.guidanceRun, zone)} · {timeLabel(document.guidanceRun, zone)}</b></div>
      <div><span>Model</span><b>TimesFM 3.0 · p50 estimate</b></div>
      <div><span>Verification source</span><b>{isLive ? 'Pending observations' : 'NOAA GHCNh'}</b></div>
    </div>
    <details className="provenance-details"><summary>Run provenance</summary>
      <dl><dt>Case</dt><dd>{document.id}</dd><dt>Model revision</dt><dd>{document.provenance.revision}</dd>
        <dt>Execution device</dt><dd>{document.provenance.device}</dd>
        {document.provenance.sha256 && <><dt>Prediction SHA-256</dt><dd>{document.provenance.sha256}</dd></>}
        {document.prospectiveFrom && <><dt>Prospective targets begin</dt><dd>{document.prospectiveFrom}</dd></>}
      </dl>
    </details>
  </div>
}

export function Stat({ label, value, unit, detail }: { label: string; value: string; unit?: string; detail: string }) {
  return <div className="stat"><span>{label}</span><div><strong>{value}</strong>{unit && <b>{unit}</b>}</div><small>{detail}</small></div>
}
