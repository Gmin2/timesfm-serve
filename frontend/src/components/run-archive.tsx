import { useState } from 'react'
import { Link } from 'react-router'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import * as Icon from './icons'
import { forecastHref } from '@/lib/navigation'
import { dateLabel, timeLabel, type Catalog, type Station, type Zone } from '@/lib/weather'

export function RunArchive({ catalog, station, zone }: { catalog: Catalog; station: Station; zone: Zone }) {
  const [filter, setFilter] = useState('all')
  const findCase = (r: Catalog['runs'][number]) => catalog.cases.find(c => c.stationId === r.station && Date.parse(c.origin) === Date.parse(r.origin))
  const rows = catalog.runs.filter(r => filter === 'all' || r.station === station.id).slice().sort((a, b) => b.origin.localeCompare(a.origin))
  const scored = rows.filter(r => findCase(r)).length
  return <section className="run-archive">
    <div className="list-toolbar">
      <div className="list-title"><h1>Run archive</h1><span className="count">{rows.length}</span></div>
      <div className="segmented" role="group" aria-label="Run station filter"><button aria-pressed={filter === 'all'} onClick={() => setFilter('all')}>All stations</button>
        <button aria-pressed={filter !== 'all'} onClick={() => setFilter('station')}>{station.name}</button></div>
      <p className="list-meta">Scheduled cases from the frozen multi-season holdout</p>
    </div>
    <Table><caption className="sr-only">All scheduled historical runs, including rejected cases.</caption><TableHeader><TableRow>
      {['Forecast origin', 'Station', 'Status', 'Observed hours', 'Result'].map(s => <TableHead scope="col" key={s}>{s}</TableHead>)}</TableRow></TableHeader>
      <TableBody>{rows.map(r => {
        const c = findCase(r)
        const origin = r.origin.replace(' ', 'T')
        const hours = Number(r.scored_hours) || 0
        return <TableRow key={r.station + r.origin}>
          <TableCell><div className="cell-stack"><span className="row-tile"><Icon.Calendar size={13} /></span><div><strong>{dateLabel(origin, zone, { year: 'numeric' })}</strong>
            <small>{timeLabel(origin, zone)} {zone === 'UTC' ? 'UTC' : 'IST'}</small></div></div></TableCell>
          <TableCell>{catalog.stations.find(s => s.id === r.station)?.name}</TableCell>
          <TableCell><span className={'run-status pill-status ' + (c ? 'accepted' : 'rejected')}>{c ? 'Scored' : 'Data rejected'}</span></TableCell>
          <TableCell>{hours ? <div className="meter-cell"><small>{hours} / 48</small><Meter value={hours} max={48} /></div> : <span className="muted">—</span>}</TableCell>
          <TableCell>{c ? <Link className="row-link" to={forecastHref(r.station, zone, 'historical_replay', c.id)}>Open forecast<Icon.ArrowUpRight size={12} /></Link>
            : <span className="muted">{r.reason.replaceAll('_', ' ')}</span>}</TableCell>
        </TableRow>
      })}</TableBody>
    </Table>
    <div className="list-footer"><span>{scored} scored · {rows.length - scored} rejected</span><span>{rows.length} runs</span></div>
  </section>
}

export function Meter({ value, max, segments = 20 }: { value: number; max: number; segments?: number }) {
  const filled = Math.round(value / max * segments)
  const tone = value / max >= 0.95 ? 'good' : value / max >= 0.6 ? 'fair' : 'low'
  return <span className={'meter ' + tone} role="img" aria-label={`${value} of ${max}`}>
    {Array.from({ length: segments }, (_, i) => <i key={i} data-on={i < filled || undefined} />)}
  </span>
}
