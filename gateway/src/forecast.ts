import type { Account } from "./accounts.js";
import { getUsage, refundCredits, reserveCredits } from "./accounts.js";
import { recordRun } from "./db.js";
import { predict } from "./inference.js";
import { log } from "./logger.js";

export const QUANTILE_LEVELS = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9];

export type ForecastInput = {
  account: Account;
  series: number[];
  horizon: number;
  pastCovariates?: number[][];
  futureCovariates?: number[][];
  requestId?: string;
};

export type ForecastBody = {
  forecast: number[];
  quantiles: number[][];
  quantile_levels: number[];
  model: string;
  credits_charged: number;
  credits_remaining: number;
};

export type ForecastResult =
  | { ok: true; body: ForecastBody; remaining: number }
  | { ok: false; remaining: number; needed: number };

/**
 * charge, run, refund on failure, log the run. credits are reserved before the
 * model runs so concurrent calls cannot each pass a check and then all spend.
 * inference errors are rethrown once the refund has landed.
 */
export async function chargedForecast(input: ForecastInput): Promise<ForecastResult> {
  const { account, series, horizon, pastCovariates, futureCovariates, requestId } = input;
  const points = horizon;

  const remaining = await reserveCredits(account.id, points);
  if (remaining === null) {
    const usage = await getUsage(account.id).catch(() => null);
    return { ok: false, remaining: usage?.credits_remaining ?? 0, needed: points };
  }

  try {
    const out = await predict({
      series: [series],
      horizon,
      ...(pastCovariates ? { past_covariates: [pastCovariates] } : {}),
      ...(futureCovariates ? { future_covariates: [futureCovariates] } : {}),
    });
    const first = out.predictions[0];
    if (!first) throw new Error("inference returned no predictions");

    // awaited on purpose. lambda freezes the execution environment as soon as
    // the response goes out, so a floating insert can be suspended and never
    // land. the catch keeps a failed ledger write from breaking a good forecast.
    await recordRun({
      accountId: account.id,
      model: out.model,
      horizon,
      contextLen: series.length,
      nPastCov: pastCovariates?.length ?? 0,
      nFutureCov: futureCovariates?.length ?? 0,
      points,
      latencyMs: out.latency_ms,
      ...(requestId ? { requestId } : {}),
    }).catch((err: unknown) => log("error", "recordRun failed", { requestId, reason: String(err) }));

    return {
      ok: true,
      remaining,
      body: {
        forecast: first.forecast,
        quantiles: first.quantiles,
        quantile_levels: QUANTILE_LEVELS,
        model: out.model,
        credits_charged: points,
        credits_remaining: remaining,
      },
    };
  } catch (err) {
    await refundCredits(account.id, points).catch((refundErr: unknown) =>
      log("error", "refund failed", {
        requestId,
        account: account.name,
        points,
        reason: String(refundErr),
      }),
    );
    throw err;
  }
}
