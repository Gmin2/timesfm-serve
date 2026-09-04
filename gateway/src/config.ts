export const config = {
  port: Number(process.env.PORT ?? 3000),
  inferenceUrl: process.env.INFERENCE_URL ?? "http://localhost:8100",
  databaseUrl: process.env.DATABASE_URL ?? "postgresql://tfm:tfm@localhost:5432/tfm_gw",
  dbPoolMax: Number(process.env.DB_POOL_MAX ?? 10),
  inferenceTimeoutMs: Number(process.env.INFERENCE_TIMEOUT_MS ?? 30_000),
  maxSeriesLength: Number(process.env.MAX_SERIES_LENGTH ?? 16_000),
  maxCovariates: Number(process.env.MAX_COVARIATES ?? 32),
  maxBodyBytes: process.env.MAX_BODY_BYTES ?? "10mb",
} as const;
