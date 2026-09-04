import { z } from "zod";

import { config } from "./config.js";

// zod 4 rejects nan and infinity in z.number() already
const finite = z.number();

export const forecastRequest = z
  .object({
    series: z.array(finite).min(8).max(config.maxSeriesLength),
    horizon: z.number().int().min(1).max(1000).default(14),
    past_covariates: z.array(z.array(finite)).max(config.maxCovariates).optional(),
    future_covariates: z.array(z.array(finite)).max(config.maxCovariates).optional(),
  })
  .strict()
  .superRefine((v, ctx) => {
    for (const row of v.past_covariates ?? []) {
      if (row.length !== v.series.length) {
        ctx.addIssue({
          code: "custom",
          path: ["past_covariates"],
          message: `each past covariate needs ${v.series.length} points, got ${row.length}`,
        });
        break;
      }
    }
    const need = v.series.length + v.horizon;
    for (const row of v.future_covariates ?? []) {
      if (row.length !== need) {
        ctx.addIssue({
          code: "custom",
          path: ["future_covariates"],
          message: `each future covariate needs ${need} points (series + horizon), got ${row.length}`,
        });
        break;
      }
    }
  });

export type ForecastRequest = z.infer<typeof forecastRequest>;

export const weatherForecastRequest = z
  .object({
    series: z.array(finite).min(8).max(config.maxSeriesLength),
    last_date: z.iso.date(),
    lat: z.number().min(-90).max(90),
    lon: z.number().min(-180).max(180),
    horizon: z.number().int().min(1).max(90).default(14),
    provider: z.string().max(40).default(config.weatherProvider),
  })
  .strict();

export type WeatherForecastRequest = z.infer<typeof weatherForecastRequest>;
