import { afterEach, describe, expect, it, vi } from "vitest";

import { OpenMeteo } from "./open-meteo.js";
import { addDays, WeatherUnavailableError } from "./types.js";

/** stands in for the open meteo http api so the tests do not touch the network */
function stubFetch(opts: { nullTail?: boolean } = {}) {
  const calls: string[] = [];
  const fake = vi.fn(async (input: string | URL | Request) => {
    const url = new URL(String(input));
    calls.push(url.origin + url.pathname);
    const start = new Date(`${url.searchParams.get("start_date")}T00:00:00Z`);
    const end = new Date(`${url.searchParams.get("end_date")}T00:00:00Z`);
    const days = Math.round((end.getTime() - start.getTime()) / 86_400_000) + 1;

    const isArchive = url.hostname.startsWith("archive");
    const values: (number | null)[] = Array.from({ length: days }, (_, i) =>
      isArchive ? 100 + i : 200 + i,
    );
    if (!isArchive && opts.nullTail) values[values.length - 1] = null;

    const daily: Record<string, unknown> = { time: Array.from({ length: days }, () => "") };
    for (const name of url.searchParams.get("daily")!.split(",")) daily[name] = values;

    return new Response(JSON.stringify({ daily }), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
  });
  vi.stubGlobal("fetch", fake);
  return calls;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("OpenMeteo", () => {
  it("uses only the archive for a range that is entirely in the past", async () => {
    const calls = stubFetch();
    const rows = await new OpenMeteo().covariates(
      12.9,
      77.6,
      new Date("2024-01-01T00:00:00Z"),
      new Date("2024-01-10T00:00:00Z"),
    );
    expect(calls).toHaveLength(1);
    expect(calls[0]).toContain("archive-api");
    expect(rows).toHaveLength(4);
    expect(rows[0]).toHaveLength(10);
  });

  it("stitches archive and forecast for a range that spans today", async () => {
    const calls = stubFetch();
    const end = addDays(new Date(), 14);
    const start = addDays(end, -30);
    const rows = await new OpenMeteo().covariates(12.9, 77.6, start, end);
    expect(calls).toHaveLength(2);
    expect(calls[0]).toContain("archive-api");
    expect(calls[1]).toContain("api.open-meteo");
    expect(rows[0]).toHaveLength(31);
    // archive half first, forecast half after
    expect(rows[0]![0]).toBe(100);
    expect(rows[0]!.at(-1)).toBeGreaterThanOrEqual(200);
  });

  it("fills nulls so no gap reaches the model", async () => {
    stubFetch({ nullTail: true });
    const end = addDays(new Date(), 14);
    const rows = await new OpenMeteo().covariates(12.9, 77.6, addDays(end, -30), end);
    expect(rows.flat().every(Number.isFinite)).toBe(true);
  });

  it("wraps a provider failure instead of leaking it", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response("nope", { status: 500 })),
    );
    await expect(
      new OpenMeteo().covariates(
        12.9,
        77.6,
        new Date("2024-01-01T00:00:00Z"),
        new Date("2024-01-05T00:00:00Z"),
      ),
    ).rejects.toBeInstanceOf(WeatherUnavailableError);
  });
});
