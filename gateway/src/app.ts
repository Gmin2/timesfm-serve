import { randomUUID } from "node:crypto";

import express, { type NextFunction, type Request, type Response } from "express";

import { requireKey } from "./auth.js";
import { config } from "./config.js";
import { recordRun } from "./db.js";
import { InferenceError, InferenceUnavailableError, predict } from "./inference.js";
import { log } from "./logger.js";
import { forecastRequest } from "./schemas.js";

export const QUANTILE_LEVELS = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9];

export const app = express();

app.use((req, res, next) => {
  req.requestId = req.header("x-request-id") ?? randomUUID();
  res.setHeader("x-request-id", req.requestId);
  const t0 = performance.now();
  res.on("finish", () => {
    log("info", "request", {
      requestId: req.requestId,
      method: req.method,
      path: req.path,
      status: res.statusCode,
      ms: Number((performance.now() - t0).toFixed(1)),
      ...(req.tenant ? { tenant: req.tenant } : {}),
    });
  });
  next();
});

app.use(express.json({ limit: config.maxBodyBytes }));

app.get("/health", (_req, res) => {
  res.json({ ok: true });
});

app.post("/v1/forecast", requireKey, async (req: Request, res: Response, next: NextFunction) => {
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

    // the response is already sent, so a failed insert must not reach next()
    recordRun({
      tenant: req.tenant ?? "unknown",
      model: out.model,
      horizon,
      contextLen: series.length,
      nPastCov: past_covariates?.length ?? 0,
      nFutureCov: future_covariates?.length ?? 0,
      latencyMs: out.latency_ms,
    }).catch((err: unknown) =>
      log("error", "recordRun failed", { requestId: req.requestId, reason: String(err) }),
    );
  } catch (err) {
    next(err);
  }
});

app.use((err: unknown, req: Request, res: Response, _next: NextFunction) => {
  const requestId = req.requestId;

  if (err instanceof InferenceError) {
    log("error", "inference error", { requestId, status: err.status, body: err.body.slice(0, 500) });
    res.status(502).json({ error: "forecast backend failed" });
    return;
  }
  if (err instanceof InferenceUnavailableError) {
    log("error", err.message, { requestId, reason: String(err.reason) });
    res.status(err.timedOut ? 504 : 502).json({ error: err.message });
    return;
  }
  log("error", "unhandled error", { requestId, reason: String(err) });
  res.status(500).json({ error: "internal error" });
});
