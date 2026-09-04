import { AwsClient } from "aws4fetch";

import { config } from "./config.js";

// the inference function url uses iam auth, so calls to it are sigv4 signed
// with the lambda's own role. locally INFERENCE_AUTH is unset and this is a
// plain fetch.
let signer: AwsClient | undefined;

function signedFetch(): typeof fetch {
  if (config.inferenceAuth !== "iam") return fetch;
  signer ??= new AwsClient({
    accessKeyId: process.env.AWS_ACCESS_KEY_ID ?? "",
    secretAccessKey: process.env.AWS_SECRET_ACCESS_KEY ?? "",
    ...(process.env.AWS_SESSION_TOKEN ? { sessionToken: process.env.AWS_SESSION_TOKEN } : {}),
    service: "lambda",
    region: process.env.AWS_REGION ?? "us-east-1",
  });
  return signer.fetch.bind(signer) as typeof fetch;
}

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
    res = await signedFetch()(`${config.inferenceUrl.replace(/\/$/, "")}/predict`, {
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
