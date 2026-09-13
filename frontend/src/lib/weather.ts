export type Method = 'forecast' | 'ecmwf' | 'ridge'
export type Series = Method | 'observed'
export type Mode = 'historical_replay' | 'experimental_live'
export type Zone = 'Asia/Kolkata' | 'UTC'
export interface Station {
  id: string; name: string; region: string; icao: string; latitude: number; longitude: number
}
export interface Point {
  time: string; forecast: number; ecmwf: number; observed: number | null; ridge: number | null
  lower: number; upper: number
}
export interface Forecast {
  id: string; stationId: string; mode: Mode; origin: string; guidanceRun: string
  quantilesCalibrated: false; points: Point[]
  generatedAt?: string; freshUntil?: string; prospectiveFrom?: string
  provenance: { model: string; revision: string; device: string; sha256: string }
}
export interface Case { id: string; stationId: string; origin: string; guidanceRun: string }
export interface Score { method: Method; n: number; mae_c: number; rmse_c: number; bias_c: number }
export interface Catalog {
  stations: Station[]; cases: Case[]
  benchmark: {
    scores: Score[]; stationScores: (Score & { stationId: string })[]
    leadScores: { lead: number; method: Method; n: number; mae_c: number; rmse_c: number }[]
    counts: { planned_origins: number; accepted_origins: number; data_rejected_origins: number; scored_hours: number; planned_hours: number }
    comparison: { rmse_skill_percent: number; rmse_delta_95ci_c: number[] }
    limits: string[]; completedAt: string; protocolSha256: string
  }
  runs: { station: string; origin: string; run: string; status: string; reason: string; scored_hours: string }[]
}
export const SERIES: Record<Series, { label: string; color: string }> = {
  forecast: { label: 'TimesFM + ECMWF', color: '#386aff' },
  ecmwf: { label: 'ECMWF IFS', color: '#f08a24' },
  observed: { label: 'NOAA observations', color: '#16151b' },
  ridge: { label: 'Ridge correction', color: '#8b5cf6' },
}
export function dateLabel(value: string | number, zone: Zone, options: Intl.DateTimeFormatOptions = {}) {
  return new Intl.DateTimeFormat('en-GB', { timeZone: zone, day: '2-digit', month: 'short', ...options }).format(new Date(value))
}
export function timeLabel(value: string | number, zone: Zone) {
  return new Intl.DateTimeFormat('en-GB', { timeZone: zone, hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }).format(new Date(value))
}
export function scorePoints(points: Point[], method: Method): Score | null {
  const errors = points.flatMap(p => p.observed === null || p[method] === null ? [] : [p[method]! - p.observed])
  if (!errors.length) return null
  return { method, n: errors.length, mae_c: errors.reduce((a, b) => a + Math.abs(b), 0) / errors.length,
    rmse_c: Math.sqrt(errors.reduce((a, b) => a + b * b, 0) / errors.length),
    bias_c: errors.reduce((a, b) => a + b, 0) / errors.length }
}
export function chartPoints(points: Point[]) {
  return points.map((point, i) => ({ ...point, timestamp: Date.parse(point.time), lead: i + 1, interval: [point.lower, point.upper] }))
}
export function downloadJson(value: unknown, name: string) {
  download(JSON.stringify(value, null, 2), name, 'application/json')
}
export function downloadCsv(points: Point[], name: string) {
  const keys: (keyof Point)[] = ['time', 'forecast', 'ecmwf', 'observed', 'ridge', 'lower', 'upper']
  download([keys.join(','), ...points.map(p => keys.map(key => p[key] ?? '').join(','))].join('\n'), name, 'text/csv')
}
function download(value: string, name: string, type: string) {
  const url = URL.createObjectURL(new Blob([value], { type }))
  const anchor = document.createElement('a')
  anchor.href = url; anchor.download = name; anchor.click()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}
export class ApiError extends Error {
  status: number
  detail: string
  constructor(status: number, detail: string) { super(detail); this.status = status; this.detail = detail }
}
export async function getJson<T>(url: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(url, { signal, headers: { Accept: 'application/json' } })
  if (!response.ok) {
    const body = await response.json().catch(() => ({}))
    throw new ApiError(response.status, body.detail || 'request_failed')
  }
  return response.json()
}
export interface LiveStatus {
  station_id: string; available: boolean; unavailable_reason: string | null
  checked_at: string; forecast_origin: string | null; fresh_until: string | null
  ingestion: { status: string; attempted_at: string; error_code: string | null } | null
  job: { status: string; attempts: number; error_code: string | null } | null
}
export function adaptLive(value: unknown, stationId: string): Forecast {
  if (!value || typeof value !== 'object') throw new ApiError(502, 'invalid_forecast_response')
  const d = value as Record<string, unknown>
  const validDate = (v: unknown) => typeof v === 'string' && Number.isFinite(Date.parse(v))
  if (d.mode !== 'experimental_live' || d.station_id !== stationId || d.horizon_hours !== 48 ||
      d.unit !== 'celsius' || d.quantiles_calibrated !== false || !validDate(d.forecast_origin) ||
      !validDate(d.fresh_until) || !validDate(d.guidance_run) || !validDate(d.generated_at) || !validDate(d.prospective_from) ||
      !Array.isArray(d.quantile_levels) || d.quantile_levels.some((q, i) => q !== (i + 1) / 10) || d.quantile_levels.length !== 9 ||
      !Array.isArray(d.points) || d.points.length !== 48) {
    throw new ApiError(502, 'invalid_forecast_response')
  }
  if (Date.parse(d.fresh_until as string) <= Date.now()) throw new ApiError(503, 'live_forecast_stale')
  const points = d.points.map((p, i): Point => {
    if (!p || typeof p !== 'object' || !validDate(p.valid_time) || Date.parse(p.valid_time) !== Date.parse(d.forecast_origin as string) + (i + 1) * 3600000 ||
        !Number.isFinite(p.temperature_2m) || !Number.isFinite(p.ecmwf_ifs_temperature_2m) ||
        !Array.isArray(p.quantiles) || p.quantiles.length !== 9 || !p.quantiles.every(Number.isFinite) ||
        p.quantiles.some((v: number, j: number) => j > 0 && v < p.quantiles[j - 1]) ||
        Math.abs(p.temperature_2m - p.quantiles[4]) > 1e-6) throw new ApiError(502, 'invalid_forecast_response')
    return { time: p.valid_time, forecast: p.temperature_2m, ecmwf: p.ecmwf_ifs_temperature_2m,
      observed: null, ridge: null, lower: p.quantiles[0], upper: p.quantiles[8] }
  })
  const provenance = (d.model_provenance ?? {}) as Record<string, unknown>
  return { id: String(d.job_id), stationId, mode: 'experimental_live', origin: d.forecast_origin as string,
    guidanceRun: d.guidance_run as string, generatedAt: String(d.generated_at),
    freshUntil: d.fresh_until as string, prospectiveFrom: String(d.prospective_from),
    quantilesCalibrated: false, points,
    provenance: { model: String(provenance.model_id ?? d.model), revision: String(provenance.revision ?? 'Not reported'),
      device: String(provenance.device ?? 'Not reported'), sha256: '' } }
}
