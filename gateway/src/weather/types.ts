/**
 * a weather provider returns covariates covering both the history window and
 * the forecast horizon, shaped [feature][step]. pravah's indus model fits this
 * contract exactly, which is the point of the interface.
 */
export type WeatherProvider = {
  readonly name: string;
  featureNames(): string[];
  /** inclusive date range, daily steps, returns [feature][day] */
  covariates(lat: number, lon: number, start: Date, end: Date): Promise<number[][]>;
};

export class WeatherUnavailableError extends Error {
  constructor(
    readonly provider: string,
    readonly reason: unknown,
  ) {
    super(`weather provider ${provider} failed`);
  }
}

export class WeatherNotImplementedError extends Error {}

export function isoDate(d: Date) {
  return d.toISOString().slice(0, 10);
}

export function addDays(d: Date, days: number) {
  const out = new Date(d);
  out.setUTCDate(out.getUTCDate() + days);
  return out;
}

export function daysBetween(a: Date, b: Date) {
  return Math.round((b.getTime() - a.getTime()) / 86_400_000);
}
