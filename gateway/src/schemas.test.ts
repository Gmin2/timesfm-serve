import { describe, expect, it } from "vitest";

import { forecastRequest, weatherForecastRequest } from "./schemas.js";

const series = Array.from({ length: 20 }, (_, i) => i + 1);

describe("forecastRequest", () => {
  it("defaults the horizon", () => {
    const r = forecastRequest.parse({ series });
    expect(r.horizon).toBe(14);
  });

  it("rejects a series shorter than 8", () => {
    expect(forecastRequest.safeParse({ series: [1, 2, 3] }).success).toBe(false);
  });

  it("rejects nan and infinity", () => {
    expect(forecastRequest.safeParse({ series: [...series.slice(1), NaN] }).success).toBe(false);
    expect(forecastRequest.safeParse({ series: [...series.slice(1), Infinity] }).success).toBe(
      false,
    );
  });

  it("rejects unknown fields so typos are not silently ignored", () => {
    expect(forecastRequest.safeParse({ series, hrizon: 7 }).success).toBe(false);
  });

  it("requires past covariates to match the series length", () => {
    expect(forecastRequest.safeParse({ series, past_covariates: [series] }).success).toBe(true);
    const bad = forecastRequest.safeParse({ series, past_covariates: [series.slice(1)] });
    expect(bad.success).toBe(false);
    expect(bad.error?.issues[0]?.message).toMatch(/needs 20 points/);
  });

  it("requires future covariates to cover series plus horizon", () => {
    const right = Array.from({ length: 20 + 7 }, () => 1);
    expect(
      forecastRequest.safeParse({ series, horizon: 7, future_covariates: [right] }).success,
    ).toBe(true);
    const bad = forecastRequest.safeParse({
      series,
      horizon: 7,
      future_covariates: [right.slice(1)],
    });
    expect(bad.success).toBe(false);
    expect(bad.error?.issues[0]?.message).toMatch(/needs 27 points/);
  });
});

describe("weatherForecastRequest", () => {
  const base = { series, last_date: "2024-03-01", lat: 12.97, lon: 77.59 };

  it("accepts a valid body and defaults provider and horizon", () => {
    const r = weatherForecastRequest.parse(base);
    expect(r.horizon).toBe(14);
    expect(r.provider).toBe("open-meteo");
  });

  it("rejects a bad date", () => {
    expect(weatherForecastRequest.safeParse({ ...base, last_date: "01-03-2024" }).success).toBe(
      false,
    );
  });

  it("rejects coordinates outside the globe", () => {
    expect(weatherForecastRequest.safeParse({ ...base, lat: 120 }).success).toBe(false);
    expect(weatherForecastRequest.safeParse({ ...base, lon: -200 }).success).toBe(false);
  });
});
