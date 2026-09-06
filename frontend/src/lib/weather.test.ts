import { describe, expect, it, vi } from 'vitest'
import { adaptLive, ApiError, chartPoints, dateLabel, scorePoints, timeLabel, type Point } from './weather'

const point: Point = { time: '2026-09-06T07:00:00Z', forecast: 21, ecmwf: 23, observed: 20, ridge: 20, lower: 19, upper: 24 }
function liveDocument() {
  return { mode: 'experimental_live', station_id: '42410099999', horizon_hours: 48, unit: 'celsius',
    quantiles_calibrated: false, forecast_origin: '2026-09-06T06:00:00Z', guidance_run: '2026-09-06T00:00:00Z',
    fresh_until: '2026-09-06T14:00:00Z', generated_at: '2026-09-06T07:00:00Z', prospective_from: '2026-09-06T08:00:00Z',
    job_id: 'test-only', model: 'timesfm_nwp', quantile_levels: [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9],
    points: Array.from({ length: 48 }, (_, i) => ({ valid_time: new Date(Date.parse('2026-09-06T06:00:00Z') + (i + 1) * 3600000).toISOString(),
      temperature_2m: 21, ecmwf_ifs_temperature_2m: 23, quantiles: [17, 18, 19, 20, 21, 22, 23, 24, 25] })) }
}
describe('forecast values and provenance', () => {
  it('does not treat missing observations as zero', () => {
    const score = scorePoints([point, { ...point, observed: null }, { ...point, observed: 0 }], 'forecast')
    expect(score?.n).toBe(2)
    expect(score?.mae_c).toBe(11)
    expect(scorePoints([{ ...point, observed: null }], 'forecast')).toBeNull()
  })
  it('preserves missing values and timestamp spacing for the chart', () => {
    const rows = chartPoints([point, { ...point, time: '2026-09-06T09:00:00Z', observed: null }])
    expect(rows[1].observed).toBeNull()
    expect(rows[1].timestamp - rows[0].timestamp).toBe(7200000)
    expect(rows[0].interval).toEqual([19, 24])
  })
  it('uses the requested timezone rather than browser defaults', () => {
    expect(timeLabel('2026-09-06T06:00:00Z', 'Asia/Kolkata')).toBe('11:30')
    expect(timeLabel('2026-09-06T06:00:00Z', 'UTC')).toBe('06:00')
    expect(dateLabel('2026-09-06T23:00:00Z', 'Asia/Kolkata')).toBe('07 Sept')
  })
  it('keeps live observations absent and does not invent model provenance', () => {
    vi.useFakeTimers(); vi.setSystemTime(new Date('2026-09-06T08:00:00Z'))
    try {
      const result = adaptLive(liveDocument(), '42410099999')
      expect(result.points).toHaveLength(48)
      expect(result.points.every(p => p.observed === null && p.ridge === null)).toBe(true)
      expect(result.provenance.device).toBe('Not reported')
    } finally { vi.useRealTimers() }
  })
  it('rejects expired data without falling back to a historical forecast', () => {
    vi.useFakeTimers(); vi.setSystemTime(new Date('2026-09-06T15:00:00Z'))
    try { expect(() => adaptLive(liveDocument(), '42410099999')).toThrow('live_forecast_stale') }
    finally { vi.useRealTimers() }
  })
  it('rejects wrong stations, non-finite temperatures and malformed timestamps', () => {
    vi.useFakeTimers(); vi.setSystemTime(new Date('2026-09-06T08:00:00Z'))
    try {
      expect(() => adaptLive(liveDocument(), '43279099999')).toThrow(ApiError)
      const bad = liveDocument(); bad.points[0].temperature_2m = NaN
      expect(() => adaptLive(bad, '42410099999')).toThrow(ApiError)
      const wrongTime = liveDocument(); wrongTime.points[0].valid_time = wrongTime.points[1].valid_time
      expect(() => adaptLive(wrongTime, '42410099999')).toThrow(ApiError)
      expect(() => adaptLive(null, '42410099999')).toThrow(ApiError)
    } finally { vi.useRealTimers() }
  })
})
