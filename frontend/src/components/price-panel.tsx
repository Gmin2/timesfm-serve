import { PriceChart } from '@/components/price-chart'
import * as Icon from '@/components/icons'
import { daySummary, rupees, type PriceForecast } from '@/lib/prices'

export function PricePanel({ document }: { document: PriceForecast }) {
  const stats = daySummary(document.blocks)
  const calibrated = document.blocks.some(b => b.q10 != null)
  const cutoff = new Date(document.cutoff_at)
  const issued = new Date(document.issued_at)
  return <>
    <section className="card chart-card">
      <div className="chart-head">
        <div className="section-heading-text">
          <h2>{longDate(document.delivery_date)}</h2>
          <p>Day-ahead clearing price, 96 blocks of 15 minutes.</p>
        </div>
        <span className={'mode-status' + (document.issued_live ? ' live' : '')}>
          <i />{document.issued_live ? 'Issued live' : 'Backfilled'}
        </span>
      </div>
      <div className="kpis price-kpis">
        <Kpi label="Day average" value={rupees(stats.mean)} unit="/MWh" note="across 96 blocks" />
        <Kpi label="Peak" value={rupees(stats.peak)} unit="/MWh" note={'at ' + stats.peakAt + ' IST'} />
        <Kpi label="Cheapest" value={rupees(stats.low)} unit="/MWh" note={'at ' + stats.lowAt + ' IST'} />
        <Kpi label="Blocks at the cap" value={String(stats.atCap)} unit="of 96" note="regulated ceiling" />
      </div>
      <PriceChart blocks={document.blocks} calibrated={calibrated} />
    </section>
    <section className="card provenance">
      <h2><Icon.Shield />How this forecast was made</h2>
      <dl>
        <div><dt>Information cutoff</dt><dd>{stamp(cutoff)} IST, before bidding opens at 10:00</dd></div>
        <div><dt>Written to the record</dt><dd>{stamp(issued)} IST</dd></div>
        <div><dt>Model</dt><dd><code>{document.model}</code></dd></div>
        <div><dt>Checkpoint</dt><dd><code>{document.model_revision.slice(0, 12)}</code>, zero-shot</dd></div>
        <div><dt>Interval</dt><dd>{calibrated ? 'p10 to p90, corrected against days that had already settled'
          : 'point forecast only'}</dd></div>
      </dl>
      <p className="notice"><Icon.Info />{document.notice}</p>
    </section>
  </>
}

function Kpi({ label, value, unit, note }: { label: string; value: string; unit: string; note: string }) {
  return <div className="stat"><span>{label}</span><div><strong>{value}</strong><b>{unit}</b></div><small>{note}</small></div>
}

function longDate(value: string): string {
  return new Date(value + 'T00:00:00Z').toLocaleDateString('en-IN',
    { weekday: 'long', day: 'numeric', month: 'long', year: 'numeric', timeZone: 'UTC' })
}

function stamp(value: Date): string {
  return value.toLocaleString('en-IN',
    { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit', hour12: false, timeZone: 'Asia/Kolkata' })
}
