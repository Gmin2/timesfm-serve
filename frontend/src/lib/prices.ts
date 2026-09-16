// Day-ahead power prices. The exchange settles 96 blocks of 15 minutes, so a
// block index is the only time axis the market actually has.

export const BLOCKS = 96
export const CAP = 10000
export const LEVELS = ['q10', 'q20', 'q30', 'q40', 'q50', 'q60', 'q70', 'q80', 'q90'] as const

export type Level = (typeof LEVELS)[number]
export type Block = { block: number; forecast: number } & Partial<Record<Level, number>>

export type PriceForecast = {
  delivery_date: string
  model: string
  issued_at: string
  cutoff_at: string
  model_revision: string
  issued_live: boolean
  blocks: Block[]
  notice: string
}

export type ScoredDay = {
  delivery_date: string
  model: string
  blocks_scored: number
  mae: number
  rmse: number
  bias: number
  coverage_p10_p90: number | null
  issued_live: boolean
}

export type Summary = { days: number; mae: number; rmse: number; coverage_p10_p90: number }
export type Scorecard = {
  summary: Summary | null
  live_only: Summary | null
  days: ScoredDay[]
  notice: string
  how_to_read: string
}

// Block 1 covers 00:00 to 00:15, so the clock is just the index in quarter hours.
export function blockTime(block: number): string {
  const minutes = (block - 1) * 15
  return String(Math.floor(minutes / 60)).padStart(2, '0') + ':' + String(minutes % 60).padStart(2, '0')
}

export function rupees(value: number): string {
  return '₹' + Math.round(value).toLocaleString('en-IN')
}

export function chartPoints(blocks: Block[]) {
  return [...blocks].sort((a, b) => a.block - b.block).map(row => ({
    block: row.block,
    time: blockTime(row.block),
    forecast: row.forecast,
    low: row.q10 ?? null,
    high: row.q90 ?? null,
    // recharts draws a band from a [low, high] pair on one key
    band: row.q10 != null && row.q90 != null ? [row.q10, row.q90] as [number, number] : null,
    atCap: row.forecast >= CAP - 0.5,
  }))
}

export function daySummary(blocks: Block[]) {
  const values = blocks.map(b => b.forecast)
  const peak = blocks.reduce((best, row) => (row.forecast > best.forecast ? row : best), blocks[0])
  const low = blocks.reduce((best, row) => (row.forecast < best.forecast ? row : best), blocks[0])
  return {
    mean: values.reduce((a, b) => a + b, 0) / values.length,
    peak: peak.forecast, peakAt: blockTime(peak.block),
    low: low.forecast, lowAt: blockTime(low.block),
    atCap: blocks.filter(b => b.forecast >= CAP - 0.5).length,
  }
}
