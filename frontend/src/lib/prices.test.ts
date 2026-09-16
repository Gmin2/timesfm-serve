import { describe, expect, it } from 'vitest'
import { blockTime, chartPoints, daySummary, rupees, BLOCKS, CAP, type Block } from './prices'

function day(values: number[], withBand = false): Block[] {
  return values.map((forecast, index) => {
    const row: Block = { block: index + 1, forecast }
    if (withBand) { row.q10 = forecast - 400; row.q90 = forecast + 400 }
    return row
  })
}

describe('block clock', () => {
  it('starts the day at midnight and ends a quarter hour short of it', () => {
    expect(blockTime(1)).toBe('00:00')
    expect(blockTime(BLOCKS)).toBe('23:45')
  })
  it('walks in quarter hours', () => {
    expect([2, 3, 4, 5].map(blockTime)).toEqual(['00:15', '00:30', '00:45', '01:00'])
  })
  it('puts the solar trough in the middle of the day', () => {
    expect(blockTime(49)).toBe('12:00')
  })
})

describe('rupees', () => {
  it('rounds and groups the indian way', () => {
    expect(rupees(1234567)).toBe('₹12,34,567')
    expect(rupees(9999.6)).toBe('₹10,000')
  })
})

describe('chart points', () => {
  it('sorts by block whatever order the api returned', () => {
    const shuffled = [{ block: 3, forecast: 30 }, { block: 1, forecast: 10 }, { block: 2, forecast: 20 }]
    expect(chartPoints(shuffled).map(p => p.block)).toEqual([1, 2, 3])
  })
  it('pairs the quantiles into one band', () => {
    const [point] = chartPoints(day([5000], true))
    expect(point.band).toEqual([4600, 5400])
  })
  it('leaves the band empty when the forecast carries no quantiles', () => {
    expect(chartPoints(day([5000]))[0].band).toBeNull()
  })
  it('marks a block sitting at the regulated cap', () => {
    const points = chartPoints(day([CAP, 2000]))
    expect(points[0].atCap).toBe(true)
    expect(points[1].atCap).toBe(false)
  })
})

describe('day summary', () => {
  const shape = Array.from({ length: BLOCKS }, (_, i) => (i >= 40 && i < 60 ? 2000 : CAP))

  it('finds the peak and the trough with the time each happened', () => {
    const stats = daySummary(day(shape))
    expect(stats.peak).toBe(CAP)
    expect(stats.low).toBe(2000)
    expect(stats.lowAt).toBe('10:00')
  })
  it('counts the blocks pinned at the cap', () => {
    expect(daySummary(day(shape)).atCap).toBe(76)
  })
  it('averages across the whole day, not just the daylight hours', () => {
    const stats = daySummary(day(shape))
    expect(stats.mean).toBeCloseTo((76 * CAP + 20 * 2000) / 96, 6)
  })
})
