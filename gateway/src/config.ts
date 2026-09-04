import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));

export const config = {
  publicDir: process.env.PUBLIC_DIR ?? join(here, "..", "public"),
  demandCsvPath: process.env.DEMAND_CSV ?? join(here, "..", "data", "demand_daily.csv"),
  port: Number(process.env.PORT ?? 3000),
  inferenceUrl: process.env.INFERENCE_URL ?? "http://localhost:8100",
  inferenceAuth: process.env.INFERENCE_AUTH ?? "none",
  databaseUrl: process.env.DATABASE_URL ?? "postgresql://tfm:tfm@localhost:5432/tfm_gw",
  dbPoolMax: Number(process.env.DB_POOL_MAX ?? 10),
  inferenceTimeoutMs: Number(process.env.INFERENCE_TIMEOUT_MS ?? 30_000),
  maxSeriesLength: Number(process.env.MAX_SERIES_LENGTH ?? 16_000),
  maxCovariates: Number(process.env.MAX_COVARIATES ?? 32),
  maxBodyBytes: process.env.MAX_BODY_BYTES ?? "10mb",
  rateLimitWindowMs: Number(process.env.RATE_LIMIT_WINDOW_MS ?? 60_000),
  weatherTimeoutMs: Number(process.env.WEATHER_TIMEOUT_MS ?? 15_000),
  weatherProvider: process.env.WEATHER_PROVIDER ?? "open-meteo",
  // not BASE_URL: vite and vitest define that themselves and set it to "/"
  baseUrl: process.env.APP_BASE_URL ?? "http://localhost:3000",
  authSecret: process.env.BETTER_AUTH_SECRET ?? "dev-secret-not-for-production",
  githubClientId: process.env.GITHUB_CLIENT_ID ?? "",
  githubClientSecret: process.env.GITHUB_CLIENT_SECRET ?? "",
  signupCredits: Number(process.env.SIGNUP_CREDITS ?? 50_000),
} as const;
