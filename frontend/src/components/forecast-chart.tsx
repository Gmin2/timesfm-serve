import { Area, ComposedChart, CartesianGrid, Line, ReferenceLine, Tooltip, XAxis, YAxis } from 'recharts'
import { ChartContainer, type ChartConfig } from '@/registry/ui/chart'
import { chartPoints, dateLabel, SERIES, timeLabel, type Point, type Series, type Zone } from '@/lib/weather'

const config = Object.fromEntries(Object.entries(SERIES).map(([key, s]) => [key, { label: s.label, colors: { light: [s.color] } }])) satisfies ChartConfig

export function ForecastChart({ points, zone, visible, showRange }: {
  points: Point[]; zone: Zone; visible: Set<Series>; showRange: boolean
}) {
  const data = chartPoints(points)
  const values = data.flatMap(p => [...visible].flatMap(key => p[key] === null ? [] : [p[key]!]))
  if (showRange) values.push(...points.flatMap(p => [p.lower, p.upper]))
  const min = Math.floor(Math.min(...values) - 1), max = Math.ceil(Math.max(...values) + 1)
  const ticks = data.filter((_, i) => i % 6 === 0).map(p => p.timestamp)
  return <ChartContainer config={config} className="forecast-chart" aria-label="48-hour temperature forecast comparison">
    <ComposedChart data={data} accessibilityLayer margin={{ top: 16, right: 20, left: -14, bottom: 12 }}>
      <CartesianGrid vertical={false} stroke="#e8eceb" strokeDasharray="3 4" />
      <XAxis dataKey="timestamp" type="number" scale="time" domain={['dataMin', 'dataMax']} ticks={ticks}
        tickFormatter={v => dateLabel(v, zone) + ' / ' + timeLabel(v, zone)} axisLine={false} tickLine={false} tickMargin={14} minTickGap={35} />
      <YAxis domain={[min, max]} tickCount={5} axisLine={false} tickLine={false} tickMargin={10} width={50} />
      {data.filter(p => timeLabel(p.timestamp, zone) === '00:00').map(p =>
        <ReferenceLine key={p.time} x={p.timestamp} stroke="#d5dcd9" strokeDasharray="4 4" />)}
      {showRange && <Area dataKey="interval" type="linear" fill={SERIES.forecast.color} fillOpacity={0.09}
        stroke="none" isAnimationActive={false} connectNulls={false} tooltipType="none" />}
      {([...visible] as Series[]).map(key => <Line key={key} dataKey={key} name={SERIES[key].label}
        stroke={SERIES[key].color} strokeWidth={key === 'forecast' ? 2.5 : 1.7}
        strokeDasharray={key === 'ecmwf' ? '6 4' : key === 'ridge' ? '3 3' : undefined}
        type="linear" dot={false} activeDot={{ r: 4, stroke: '#fff', strokeWidth: 2 }}
        connectNulls={false} isAnimationActive={false} />)}
      <Tooltip cursor={{ stroke: '#9ea9a4', strokeDasharray: '3 3' }}
        content={({ active, payload }) => {
          const point = payload?.[0]?.payload as (typeof data)[number] | undefined
          if (!active || !point) return null
          return <div className="chart-tooltip">
            <strong>{dateLabel(point.time, zone)} <span>{timeLabel(point.time, zone)} {zone === 'UTC' ? 'UTC' : 'IST'}</span></strong>
            {[...visible].map(key => <div key={key}><i style={{ background: SERIES[key].color }} />{SERIES[key].label}
              <b>{point[key] === null ? 'No observation' : point[key]!.toFixed(1) + ' °C'}</b></div>)}
            {showRange && <small>p10–p90: {point.lower.toFixed(1)}–{point.upper.toFixed(1)} °C · Uncalibrated</small>}
          </div>
        }} />
    </ComposedChart>
  </ChartContainer>
}
