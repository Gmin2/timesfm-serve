import request from "supertest";
import { afterAll, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { findOrCreateAccount, refundCredits, reserveCredits } from "./accounts.js";
import { app } from "./app.js";
import { createApiKey } from "./auth.js";
import { migrate, pool, query } from "./db.js";
import * as inference from "./inference.js";

let accountId: number;
let key: string;

const series = Array.from({ length: 20 }, (_, i) => 1000 + i);

function fakePrediction(horizon: number) {
  return {
    predictions: [
      {
        forecast: Array.from({ length: horizon }, () => 1),
        quantiles: Array.from({ length: horizon }, () => Array.from({ length: 9 }, () => 1)),
      },
    ],
    model: "test-model",
    device: "cpu",
    latency_ms: 1,
  };
}

beforeAll(async () => {
  await migrate();
});

afterAll(async () => {
  await pool.end();
});

beforeEach(async () => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  await query("delete from forecast_runs");
  await query("delete from rate_limit_counters");
  await query("delete from api_keys");
  await query("delete from accounts");
  accountId = await findOrCreateAccount(`acct-${Date.now()}-${Math.random()}`);
  key = await createApiKey(accountId, "test");
});

describe("credits", () => {
  it("refuses to go past the grant", async () => {
    await query("update accounts set credits_granted = 30, credits_used = 0 where id = $1", [
      accountId,
    ]);
    expect(await reserveCredits(accountId, 20)).toBe(10);
    expect(await reserveCredits(accountId, 20)).toBeNull();
    expect(await reserveCredits(accountId, 10)).toBe(0);
  });

  it("never oversells under concurrency", async () => {
    await query("update accounts set credits_granted = 100, credits_used = 0 where id = $1", [
      accountId,
    ]);
    const results = await Promise.all(
      Array.from({ length: 20 }, () => reserveCredits(accountId, 10)),
    );
    const granted = results.filter((r) => r !== null).length;
    expect(granted).toBe(10);
    const { rows } = await query<{ credits_used: number }>(
      "select credits_used from accounts where id = $1",
      [accountId],
    );
    expect(rows[0]?.credits_used).toBe(100);
  });

  it("refunds without going below zero", async () => {
    await query("update accounts set credits_used = 5 where id = $1", [accountId]);
    await refundCredits(accountId, 50);
    const { rows } = await query<{ credits_used: number }>(
      "select credits_used from accounts where id = $1",
      [accountId],
    );
    expect(rows[0]?.credits_used).toBe(0);
  });

  it("refuses a suspended account", async () => {
    await query("update accounts set suspended_at = now() where id = $1", [accountId]);
    expect(await reserveCredits(accountId, 1)).toBeNull();
  });
});

describe("auth", () => {
  it("rejects a missing key", async () => {
    await request(app).post("/v1/forecast").send({ series }).expect(401);
  });

  it("rejects an unknown key", async () => {
    await request(app)
      .post("/v1/forecast")
      .set("x-api-key", "tfm_not_a_real_key")
      .send({ series })
      .expect(401);
  });

  it("rejects a revoked key", async () => {
    await query("update api_keys set revoked_at = now() where account_id = $1", [accountId]);
    await request(app).post("/v1/forecast").set("x-api-key", key).send({ series }).expect(401);
  });

  it("never stores the key itself", async () => {
    const { rows } = await query<{ hash: string; prefix: string }>(
      "select hash, prefix from api_keys where account_id = $1",
      [accountId],
    );
    expect(rows[0]?.hash).not.toBe(key);
    expect(rows[0]?.hash).toHaveLength(64);
    expect(key.startsWith(rows[0]!.prefix)).toBe(true);
  });
});

describe("forecast route", () => {
  it("charges, answers and logs a run", async () => {
    vi.spyOn(inference, "predict").mockResolvedValue(fakePrediction(14));

    const res = await request(app)
      .post("/v1/forecast")
      .set("x-api-key", key)
      .send({ series, horizon: 14 })
      .expect(200);

    expect(res.body.forecast).toHaveLength(14);
    expect(res.body.credits_charged).toBe(14);
    expect(res.headers["x-credits-remaining"]).toBe(String(50000 - 14));

    const { rows } = await query<{ points: number; context_len: number }>(
      "select points, context_len from forecast_runs where account_id = $1",
      [accountId],
    );
    expect(rows[0]).toMatchObject({ points: 14, context_len: 20 });
  });

  it("refunds when inference fails", async () => {
    vi.spyOn(inference, "predict").mockRejectedValue(
      new inference.InferenceUnavailableError(new Error("down"), false),
    );

    await request(app)
      .post("/v1/forecast")
      .set("x-api-key", key)
      .send({ series, horizon: 14 })
      .expect(502);

    const { rows } = await query<{ credits_used: number }>(
      "select credits_used from accounts where id = $1",
      [accountId],
    );
    expect(rows[0]?.credits_used).toBe(0);
  });

  it("returns 402 when the grant is spent", async () => {
    await query("update accounts set credits_granted = 5 where id = $1", [accountId]);
    const res = await request(app)
      .post("/v1/forecast")
      .set("x-api-key", key)
      .send({ series, horizon: 14 })
      .expect(402);
    expect(res.body).toMatchObject({ error: "out of credits", credits_needed: 14 });
  });

  it("rate limits per key and does not charge the rejected calls", async () => {
    // the limiter uses a wall clock fixed window, so without pinning the clock
    // this test resets its own window whenever it straddles a minute boundary
    vi.useFakeTimers({ toFake: ["Date"], now: new Date("2026-01-01T00:00:10Z") });
    vi.spyOn(inference, "predict").mockResolvedValue(fakePrediction(1));
    await query("update accounts set rate_limit_per_min = 2 where id = $1", [accountId]);

    const codes: number[] = [];
    for (let i = 0; i < 4; i++) {
      const res = await request(app)
        .post("/v1/forecast")
        .set("x-api-key", key)
        .send({ series, horizon: 1 });
      codes.push(res.status);
    }
    expect(codes).toEqual([200, 200, 429, 429]);

    const { rows } = await query<{ credits_used: number }>(
      "select credits_used from accounts where id = $1",
      [accountId],
    );
    expect(rows[0]?.credits_used).toBe(2);
    vi.useRealTimers();
  });
});

describe("usage", () => {
  it("reports the balance", async () => {
    await query("update accounts set credits_granted = 100, credits_used = 25 where id = $1", [
      accountId,
    ]);
    const res = await request(app).get("/v1/usage").set("x-api-key", key).expect(200);
    expect(res.body).toMatchObject({
      credits_granted: 100,
      credits_used: 25,
      credits_remaining: 75,
    });
  });
});
