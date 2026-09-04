import { config } from "../config.js";
import {
  addDays,
  daysBetween,
  isoDate,
  type WeatherProvider,
  WeatherUnavailableError,
} from "./types.js";

const DAILY_VARS = [
  "temperature_2m_mean",
  "temperature_2m_max",
  "shortwave_radiation_sum",
  "wind_speed_10m_max",
] as const;

const ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive";
const FORECAST_URL = "https://api.open-meteo.com/v1/forecast";

// the archive lags real time by a few days, the forecast api covers roughly the
// last 90 days plus 16 ahead. so a range that spans both has to be stitched.
const ARCHIVE_LAG_DAYS = 7;

type DailyBlock = Record<string, (number | null)[]> & { time: string[] };

async function fetchDaily(
  url: string,
  lat: number,
  lon: number,
  start: Date,
  end: Date,
): Promise<DailyBlock> {
  const params = new URLSearchParams({
    latitude: String(lat),
    longitude: String(lon),
    start_date: isoDate(start),
    end_date: isoDate(end),
    timezone: "UTC",
    daily: DAILY_VARS.join(","),
  });
  const res = await fetch(`${url}?${params.toString()}`, {
    signal: AbortSignal.timeout(config.weatherTimeoutMs),
  });
  if (!res.ok) throw new Error(`${url} returned ${res.status}: ${(await res.text()).slice(0, 200)}`);
  const body = (await res.json()) as { daily?: DailyBlock; reason?: string };
  if (!body.daily) throw new Error(body.reason ?? "no daily block in response");
  return body.daily;
}

/** carry the last known value forward, then backward, so no nulls reach the model */
function fill(row: (number | null)[]): number[] {
  const out = row.slice();
  let last: number | null = null;
  for (let i = 0; i < out.length; i++) {
    if (out[i] == null) out[i] = last;
    else last = out[i]!;
  }
  last = null;
  for (let i = out.length - 1; i >= 0; i--) {
    if (out[i] == null) out[i] = last;
    else last = out[i]!;
  }
  return out.map((v) => v ?? 0);
}

export class OpenMeteo implements WeatherProvider {
  readonly name = "open-meteo";

  featureNames() {
    return [...DAILY_VARS];
  }

  async covariates(lat: number, lon: number, start: Date, end: Date): Promise<number[][]> {
    const split = new Date(
      Math.min(end.getTime(), addDays(new Date(), -ARCHIVE_LAG_DAYS).getTime()),
    );

    try {
      const blocks: DailyBlock[] = [];
      if (start <= split) blocks.push(await fetchDaily(ARCHIVE_URL, lat, lon, start, split));
      if (end > split) {
        const from = start > split ? start : addDays(split, 1);
        blocks.push(await fetchDaily(FORECAST_URL, lat, lon, from, end));
      }

      const rows = DAILY_VARS.map((name) =>
        fill(blocks.flatMap((b) => b[name] ?? new Array<null>(b.time.length).fill(null))),
      );

      const expected = daysBetween(start, end) + 1;
      const got = rows[0]?.length ?? 0;
      if (got !== expected) {
        throw new Error(`expected ${expected} days of weather, provider returned ${got}`);
      }
      return rows;
    } catch (err) {
      throw new WeatherUnavailableError(this.name, err);
    }
  }
}
