import express, { type NextFunction, type Request, type Response } from "express";

import { config } from "./config.js";
import { InferenceError, InferenceUnavailableError, predict } from "./inference.js";
import { forecastRequest } from "./schemas.js";

export const QUANTILE_LEVELS = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9];

export const app = express();
app.use(express.json({ limit: config.maxBodyBytes }));

app.get("/health", (_req, res) => {
  res.json({ ok: true });
});

app.post("/v1/forecast", async (req: Request, res: Response, next: NextFunction) => {
  const parsed = forecastRequest.safeParse(req.body);
  if (!parsed.success) {
    res.status(422).json({ error: "invalid request", details: parsed.error.issues });
    return;
  }

  const { series, horizon, past_covariates, future_covariates } = parsed.data;
  try {
    const out = await predict({
      series: [series],
      horizon,
      ...(past_covariates ? { past_covariates: [past_covariates] } : {}),
      ...(future_covariates ? { future_covariates: [future_covariates] } : {}),
    });
    const first = out.predictions[0];
    if (!first) throw new Error("inference returned no predictions");
    res.json({
      forecast: first.forecast,
      quantiles: first.quantiles,
      quantile_levels: QUANTILE_LEVELS,
      model: out.model,
    });
  } catch (err) {
    next(err);
  }
});

app.use((err: unknown, _req: Request, res: Response, _next: NextFunction) => {
  if (err instanceof InferenceError) {
    console.error(
      JSON.stringify({
        level: "error",
        msg: "inference error",
        status: err.status,
        body: err.body.slice(0, 500),
      }),
    );
    res.status(502).json({ error: "forecast backend failed" });
    return;
  }
  if (err instanceof InferenceUnavailableError) {
    console.error(
      JSON.stringify({ level: "error", msg: err.message, reason: String(err.reason) }),
    );
    res.status(err.timedOut ? 504 : 502).json({ error: err.message });
    return;
  }
  console.error(JSON.stringify({ level: "error", msg: String(err) }));
  res.status(500).json({ error: "internal error" });
});
