import { randomUUID } from "node:crypto";

import express, { type NextFunction, type Request, type Response } from "express";

import { getUsage, refundCredits, reserveCredits } from "./accounts.js";
import { requireKey } from "./auth.js";
import { config } from "./config.js";
import { recordRun } from "./db.js";
import { InferenceError, InferenceUnavailableError, predict } from "./inference.js";
import { log } from "./logger.js";
import { rateLimit } from "./rate-limit.js";
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
      ...(req.account ? { account: req.account.name } : {}),
    });
  });
  next();
});

app.use(express.json({ limit: config.maxBodyBytes }));

app.get("/health", (_req, res) => {
  res.json({ ok: true });
});

app.get("/v1/usage", requireKey, async (req: Request, res: Response, next: NextFunction) => {
  try {
    const usage = await getUsage(req.account!.id);
    if (!usage) {
      res.status(404).json({ error: "account not found" });
      return;
    }
    res.json(usage);
  } catch (err) {
    next(err);
  }
});

app.post(
  "/v1/forecast",
  requireKey,
  rateLimit,
  async (req: Request, res: Response, next: NextFunction) => {
    const parsed = forecastRequest.safeParse(req.body);
    if (!parsed.success) {
      res.status(422).json({ error: "invalid request", details: parsed.error.issues });
      return;
    }

    const account = req.account!;
    const { series, horizon, past_covariates, future_covariates } = parsed.data;

    // one series through this endpoint, so the cost is just the horizon.
    // charged before the model runs, refunded below if it fails.
    const points = horizon;

    let remaining: number | null;
    try {
      remaining = await reserveCredits(account.id, points);
    } catch (err) {
      next(err);
      return;
    }
    if (remaining === null) {
      const usage = await getUsage(account.id).catch(() => null);
      res.status(402).json({
        error: "out of credits",
        credits_remaining: usage?.credits_remaining ?? 0,
        credits_needed: points,
      });
      return;
    }
    res.setHeader("x-credits-remaining", remaining);

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
        credits_charged: points,
        credits_remaining: remaining,
      });

      // the response is already sent, so a failed insert must not reach next()
      recordRun({
        accountId: account.id,
        model: out.model,
        horizon,
        contextLen: series.length,
        nPastCov: past_covariates?.length ?? 0,
        nFutureCov: future_covariates?.length ?? 0,
        points,
        latencyMs: out.latency_ms,
        ...(req.requestId ? { requestId: req.requestId } : {}),
      }).catch((err: unknown) =>
        log("error", "recordRun failed", { requestId: req.requestId, reason: String(err) }),
      );
    } catch (err) {
      await refundCredits(account.id, points).catch((refundErr: unknown) =>
        log("error", "refund failed", {
          requestId: req.requestId,
          account: account.name,
          points,
          reason: String(refundErr),
        }),
      );
      next(err);
    }
  },
);

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
