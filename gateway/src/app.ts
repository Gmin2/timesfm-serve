import { randomUUID } from "node:crypto";

import express, { type NextFunction, type Request, type Response } from "express";

import { getUsage } from "./accounts.js";
import { requireKey } from "./auth.js";
import { config } from "./config.js";
import { chargedForecast } from "./forecast.js";
import { InferenceError, InferenceUnavailableError } from "./inference.js";
import { log } from "./logger.js";
import { rateLimit } from "./rate-limit.js";
import { forecastRequest, weatherForecastRequest } from "./schemas.js";
import {
  addDays,
  getProvider,
  providerNames,
  WeatherNotImplementedError,
  WeatherUnavailableError,
} from "./weather/index.js";

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

app.get("/v1/weather/providers", (_req, res) => {
  res.json({ providers: providerNames(), default: config.weatherProvider });
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

    const { series, horizon, past_covariates, future_covariates } = parsed.data;
    try {
      const result = await chargedForecast({
        account: req.account!,
        series,
        horizon,
        ...(past_covariates ? { pastCovariates: past_covariates } : {}),
        ...(future_covariates ? { futureCovariates: future_covariates } : {}),
        ...(req.requestId ? { requestId: req.requestId } : {}),
      });
      if (!result.ok) {
        res.status(402).json({
          error: "out of credits",
          credits_remaining: result.remaining,
          credits_needed: result.needed,
        });
        return;
      }
      res.setHeader("x-credits-remaining", result.remaining);
      res.json(result.body);
    } catch (err) {
      next(err);
    }
  },
);

/**
 * the same forecast, but the caller sends only demand history and a location
 * and the service fetches the weather covariates itself. this is the endpoint
 * a real weather model like indus would sit behind.
 */
app.post(
  "/v1/forecast/weather",
  requireKey,
  rateLimit,
  async (req: Request, res: Response, next: NextFunction) => {
    const parsed = weatherForecastRequest.safeParse(req.body);
    if (!parsed.success) {
      res.status(422).json({ error: "invalid request", details: parsed.error.issues });
      return;
    }

    const { series, last_date, lat, lon, horizon, provider: providerName } = parsed.data;
    const provider = getProvider(providerName);
    if (!provider) {
      res.status(400).json({ error: `unknown weather provider ${providerName}` });
      return;
    }

    const lastDate = new Date(`${last_date}T00:00:00Z`);
    const start = addDays(lastDate, -(series.length - 1));
    const end = addDays(lastDate, horizon);

    try {
      const futureCovariates = await provider.covariates(lat, lon, start, end);
      const result = await chargedForecast({
        account: req.account!,
        series,
        horizon,
        futureCovariates,
        ...(req.requestId ? { requestId: req.requestId } : {}),
      });
      if (!result.ok) {
        res.status(402).json({
          error: "out of credits",
          credits_remaining: result.remaining,
          credits_needed: result.needed,
        });
        return;
      }
      res.setHeader("x-credits-remaining", result.remaining);
      res.json({
        ...result.body,
        weather_provider: provider.name,
        weather_features: provider.featureNames(),
      });
    } catch (err) {
      next(err);
    }
  },
);

app.use((err: unknown, req: Request, res: Response, _next: NextFunction) => {
  const requestId = req.requestId;

  if (err instanceof WeatherNotImplementedError) {
    res.status(501).json({ error: err.message });
    return;
  }
  if (err instanceof WeatherUnavailableError) {
    log("error", "weather provider failed", {
      requestId,
      provider: err.provider,
      reason: String(err.reason),
    });
    res.status(502).json({ error: `weather provider ${err.provider} failed` });
    return;
  }
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
