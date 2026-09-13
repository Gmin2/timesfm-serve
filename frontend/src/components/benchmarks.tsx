import { Bar, BarChart, CartesianGrid, Cell, LabelList, Line, LineChart, Tooltip, XAxis, YAxis } from 'recharts'
import { ChartContainer } from '@/registry/ui/chart'
import { Button } from './ui/button'
import { CopyButton } from './copy-button'
import * as Icon from './icons'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from './ui/table'
import { Stat } from './forecast-panel'
import { downloadJson, SERIES, type Catalog, type Method } from '@/lib/weather'

const config = Object.fromEntries(Object.entries(SERIES).map(([key, s]) => [key, { label: s.label, colors: { light: [s.color] } }]))
export function Benchmarks({ catalog }: { catalog: Catalog }) {
  const b = catalog.benchmark
  const methods: Method[] = ['ecmwf', 'forecast', 'ridge']
  const rows = methods.map(method => ({ ...b.scores.find(s => s.method === method)!, label: SERIES[method].label }))
  const leads = Array.from({ length: 48 }, (_, i) => Object.fromEntries([
    ['lead', i + 1], ...methods.map(method => [method, b.leadScores.find(s => s.lead === i + 1 && s.method === method)?.rmse_c ?? null]),
  ]))
  return <div className="benchmarks">
    <div className="page-head">
      <span className="page-tile"><Icon.Flask size={18} /></span>
      <div className="page-title"><h1>Forecast benchmarks</h1><span className="badge">Retrospective</span></div>
      <p>Multi-season holdout · December 2025 – August 2026 · three airport stations, same observed hours for every method</p>
      <div className="page-actions"><Button variant="dark" size="sm" onClick={() => downloadJson(b, 'weather-holdout-benchmark.json')}><Icon.Download />Export results</Button></div>
    </div>
    <div className="id-box"><span>protocol</span><code>{b.protocolSha256}</code><CopyButton value={b.protocolSha256} label="Copy protocol hash" /></div>
    <div className="card kpis">
      <Stat label="Lower RMSE vs ECMWF" value={b.comparison.rmse_skill_percent.toFixed(1)} unit="%" detail="TimesFM + ECMWF correction" />
      <Stat label="Matched observations" value={b.counts.scored_hours.toLocaleString()} detail="Hourly temperature targets" />
      <Stat label="Accepted cases" value={String(b.counts.accepted_origins)} unit={'/ ' + b.counts.planned_origins} detail={b.counts.data_rejected_origins + ' data-rejected cases'} />
      <Stat label="Best holdout RMSE" value={Math.min(...rows.map(s => s.rmse_c)).toFixed(3)} unit="°C" detail="Ridge correction" />
    </div>
    <h2 className="section-title">Model comparison</h2>
    <div className="benchmark-charts">
      <section className="card"><div className="section-heading"><div><h3>RMSE by method</h3><p>All stations · lower is better</p></div></div>
        <ChartContainer config={config} className="benchmark-bar">
          <BarChart data={rows} layout="vertical" margin={{ left: 0, right: 45, bottom: 12 }} accessibilityLayer>
            <CartesianGrid horizontal={false} stroke="#efeeeb" strokeDasharray="3 4" />
            <XAxis type="number" domain={[0, 2]} tickLine={false} axisLine={false} unit="°" />
            <YAxis type="category" dataKey="label" width={136} tickLine={false} axisLine={false} tick={{ fontSize: 11 }} />
            <Tooltip cursor={false} formatter={value => [Number(value).toFixed(3) + ' °C', 'RMSE']} />
            <Bar dataKey="rmse_c" barSize={20} radius={[0, 4, 4, 0]} isAnimationActive={false}>
              {rows.map(row => <Cell key={row.method} fill={SERIES[row.method].color} />)}
              <LabelList dataKey="rmse_c" position="right" formatter={value => Number(value).toFixed(3)} fill="#3f3e3c" fontSize={11} />
            </Bar>
          </BarChart>
        </ChartContainer>
      </section>
      <section className="card"><div className="section-heading"><div><h3>Error by forecast hour</h3><p>All three stations · RMSE (°C)</p></div></div>
        <ChartContainer config={config} className="benchmark-line">
          <LineChart data={leads} margin={{ left: -20, right: 12, top: 10, bottom: 12 }} accessibilityLayer>
            <CartesianGrid vertical={false} stroke="#efeeeb" strokeDasharray="3 4" />
            <XAxis dataKey="lead" type="number" domain={[1, 48]} ticks={[1, 12, 24, 36, 48]} tickFormatter={v => '+' + v + 'h'} tickLine={false} axisLine={false} />
            <YAxis tickLine={false} axisLine={false} />
            <Tooltip labelFormatter={v => 'Forecast hour +' + v} formatter={(v, name) => [Number(v).toFixed(3) + ' °C', name]} />
            {methods.map(m => <Line key={m} name={SERIES[m].label} dataKey={m} stroke={SERIES[m].color}
              strokeWidth={1.8} dot={false} type="linear" connectNulls={false} isAnimationActive={false} />)}
          </LineChart>
        </ChartContainer>
      </section>
    </div>
    <section className="scores-section"><h2 className="section-title">Station-level results</h2>
      <div className="card table-card"><Table><caption className="sr-only">Station holdout scores, lower RMSE and MAE are better.</caption>
        <TableHeader><TableRow>{['Station', 'Method', 'RMSE (°C)', 'MAE (°C)', 'Bias (°C)', 'Matched hours', 'vs ECMWF'].map(t => <TableHead scope="col" key={t}>{t}</TableHead>)}</TableRow></TableHeader>
        <TableBody>{catalog.stations.flatMap(station => methods.map((method, i) => {
          const row = b.stationScores.find(s => s.stationId === station.id && s.method === method)!
          const base = b.stationScores.find(s => s.stationId === station.id && s.method === 'ecmwf')!.rmse_c
          const improvement = (1 - row.rmse_c / base) * 100
          return <TableRow key={station.id + method} className={i === 0 ? 'station-divider' : ''}>
            <TableCell>{i === 0 && <strong>{station.name}</strong>}</TableCell>
            <TableCell><span className="method-label"><i style={{ background: SERIES[method].color }} />{SERIES[method].label}</span></TableCell>
            <TableCell>{row.rmse_c.toFixed(3)}</TableCell><TableCell>{row.mae_c.toFixed(3)}</TableCell>
            <TableCell>{row.bias_c > 0 ? '+' : ''}{row.bias_c.toFixed(3)}</TableCell><TableCell>{row.n.toLocaleString()}</TableCell>
            <TableCell>{method === 'ecmwf' ? <span className="muted">Baseline</span> : <span className={improvement >= 0 ? 'improvement' : 'regression'}>
              {Math.abs(improvement).toFixed(1)}% {improvement >= 0 ? 'lower' : 'higher'}</span>}</TableCell>
          </TableRow>
        }))}</TableBody>
      </Table></div>
    </section>
    <div className="benchmark-limits"><h2 className="section-title">Scope of these results</h2><p>Ridge slightly outperformed TimesFM on RMSE. These retrospective results do not establish live, India-wide, or Indus-wx superiority.</p>
      <p>Paired 95% interval for the RMSE difference: {b.comparison.rmse_delta_95ci_c.map(n => n.toFixed(3)).join(' to ')} °C. Quantile calibration is not established.</p>
      <details><summary>Protocol and limitations</summary><ul>{b.limits.map(limit => <li key={limit}>{limit}</li>)}</ul>
</details>
    </div>
  </div>
}
