import { Area, CartesianGrid, ComposedChart, Line, ReferenceLine, Tooltip, XAxis, YAxis } from 'recharts'
import { ChartContainer, type ChartConfig } from '@/registry/ui/chart'
import { CAP, chartPoints, rupees, type Block } from '@/lib/prices'

const config = {
  forecast: { label: 'Forecast', colors: { light: ['#0063e6'] } },
  band: { label: 'p10 to p90', colors: { light: ['#386aff'] } },
} satisfies ChartConfig

export function PriceChart({ blocks, calibrated }: { blocks: Block[]; calibrated: boolean }) {
  const data = chartPoints(blocks)
  const spread = data.flatMap(p => [p.forecast, ...(p.band ?? [])])
  const top = Math.min(CAP, Math.ceil(Math.max(...spread) / 500) * 500)
  const floor = Math.max(0, Math.floor(Math.min(...spread) / 500) * 500 - 250)
  // one tick every two hours keeps 96 blocks readable
  const ticks = data.filter(p => (p.block - 1) % 8 === 0).map(p => p.block)
  return <ChartContainer config={config} className="forecast-chart" aria-label="Day-ahead price forecast, 96 blocks">
    <ComposedChart data={data} accessibilityLayer margin={{ top: 16, right: 20, left: 4, bottom: 12 }}>
      <CartesianGrid vertical={false} stroke="#efeeeb" strokeDasharray="3 4" />
      <XAxis dataKey="block" type="number" domain={[1, 96]} ticks={ticks}
        tickFormatter={block => data[block - 1]?.time ?? ''} axisLine={false} tickLine={false}
        tickMargin={14} minTickGap={28} />
      <YAxis domain={[floor, top]} tickCount={5} axisLine={false} tickLine={false} tickMargin={10}
        width={62} tickFormatter={value => rupees(value)} />
      <ReferenceLine y={CAP} stroke="#fd2c37" strokeDasharray="4 4"
        label={{ value: 'CERC cap', position: 'insideTopRight', fill: '#7a7976', fontSize: 11 }} />
      <Area dataKey="band" type="linear" fill="#386aff" fillOpacity={0.09} stroke="none"
        isAnimationActive={false} connectNulls={false} tooltipType="none" />
      <Line dataKey="forecast" name="Forecast" stroke="#0063e6" strokeWidth={2} type="linear"
        dot={false} activeDot={{ r: 4, stroke: '#fff', strokeWidth: 2 }} isAnimationActive={false} />
      <Tooltip cursor={{ stroke: '#c9c8c4', strokeDasharray: '3 3' }}
        content={({ active, payload }) => {
          const point = payload?.[0]?.payload as (typeof data)[number] | undefined
          if (!active || !point) return null
          return <div className="chart-tooltip">
            <strong>Block {point.block} <span>{point.time} IST</span></strong>
            <div><i style={{ background: '#0063e6' }} />Forecast<b>{rupees(point.forecast)}</b></div>
            {point.band && <small>p10&ndash;p90: {rupees(point.band[0])}&ndash;{rupees(point.band[1])}
              {calibrated ? ' · calibrated' : ' · uncalibrated'}</small>}
            {point.atCap && <small>at the regulated cap</small>}
          </div>
        }} />
    </ComposedChart>
  </ChartContainer>
}
