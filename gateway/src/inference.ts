import { config } from "./config.js";

export type Prediction = { forecast: number[]; quantiles: number[][] };

export type PredictResponse = {
  predictions: Prediction[];
  model: string;
  device: string;
  latency_ms: number;
};

export type PredictBody = {
  series: number[][];
  horizon: number;
  past_covariates?: (number[][] | null)[];
  future_covariates?: (number[][] | null)[];
};

export class InferenceError extends Error {
  constructor(
    readonly status: number,
    readonly body: string,
  ) {
    super(`inference failed with ${status}`);
  }
}

export class InferenceUnavailableError extends Error {
  constructor(
    readonly reason: unknown,
    readonly timedOut: boolean,
  ) {
    super(timedOut ? "inference timed out" : "inference unreachable");
  }
}

export async function predict(body: PredictBody): Promise<PredictResponse> {
  let res: Response;
  try {
    res = await fetch(`${config.inferenceUrl}/predict`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(config.inferenceTimeoutMs),
    });
  } catch (err) {
    const timedOut =
      err instanceof Error && (err.name === "TimeoutError" || err.name === "AbortError");
    throw new InferenceUnavailableError(err, timedOut);
  }
  if (!res.ok) throw new InferenceError(res.status, await res.text());
  return (await res.json()) as PredictResponse;
}
