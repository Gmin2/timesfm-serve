import * as Icon from '@/components/icons'
import { rupees, type Scorecard, type Summary } from '@/lib/prices'

export function PriceScorecard({ card }: { card: Scorecard }) {
  if (!card.summary) {
    return <div className="empty-state" role="status"><span className="empty-icon"><Icon.Clock size={20} /></span>
      <h2>No day has settled yet</h2><p>Forecasts are scored once the exchange publishes all 96 blocks.</p></div>
  }
  const backfilled = card.summary.days - (card.live_only?.days ?? 0)
  return <>
    <section className="card">
      <div className="kpis">
        <Kpi label="Days scored" value={String(card.summary.days)} unit="days"
          note={backfilled ? backfilled + ' backfilled' : 'all issued live'} />
        <Kpi label="Mean absolute error" value={rupees(card.summary.mae)} unit="/MWh" note="lower is better" />
        <Kpi label="RMSE" value={rupees(card.summary.rmse)} unit="/MWh" note="penalises big misses" />
        <Kpi label="p10 to p90 band" value={percent(card.summary.coverage_p10_p90)} unit="held"
          note="should hold 80%" />
      </div>
      {card.live_only && <p className="notice"><Icon.Signal />
        Issued live only: {card.live_only.days} days, {rupees(card.live_only.mae)} MAE.</p>}
      <p className="notice"><Icon.Info />{card.how_to_read}</p>
    </section>
    <section className="card table-card">
      <table>
        <thead><tr><th>Delivery day</th><th className="num">MAE</th><th className="num">RMSE</th>
          <th className="num">Bias</th><th className="num">Band held</th><th>Issued</th></tr></thead>
        <tbody>
          {card.days.map(day => <tr key={day.delivery_date + day.model}>
            <td>{shortDate(day.delivery_date)}</td>
            <td className="num">{rupees(day.mae)}</td>
            <td className="num">{rupees(day.rmse)}</td>
            <td className={'num ' + (day.bias >= 0 ? 'up' : 'down')}>{day.bias >= 0 ? '+' : '−'}{rupees(Math.abs(day.bias))}</td>
            <td className="num">{day.coverage_p10_p90 == null ? '—' : percent(day.coverage_p10_p90)}</td>
            <td><span className={'pill-status ' + (day.issued_live ? 'accepted' : 'revoked')}>{day.issued_live ? 'live' : 'backfill'}</span></td>
          </tr>)}
        </tbody>
      </table>
    </section>
  </>
}

function Kpi({ label, value, unit, note }: { label: string; value: string; unit: string; note: string }) {
  return <div className="stat"><span>{label}</span><div><strong>{value}</strong><b>{unit}</b></div><small>{note}</small></div>
}
function percent(value: number): string { return (value * 100).toFixed(1) + '%' }
function shortDate(value: string): string {
  return new Date(value + 'T00:00:00Z').toLocaleDateString('en-IN',
    { weekday: 'short', day: 'numeric', month: 'short', timeZone: 'UTC' })
}
export type { Summary }
