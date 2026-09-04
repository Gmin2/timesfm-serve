import { type WeatherProvider, WeatherNotImplementedError } from "./types.js";

/**
 * pravah's indus weather model. it publishes exactly what a grid forecast
 * needs: irradiance, wind at turbine height and temperature, hourly, 3 km,
 * for india. the api is not public yet, so this is the shape it would take.
 *
 * to wire it up, implement covariates() against their endpoint and return
 * [feature][step] aligned to the same daily grid the caller asked for.
 */
export class Indus implements WeatherProvider {
  readonly name = "indus";

  featureNames() {
    return ["ghi", "wind_speed_100m", "temperature_2m"];
  }

  async covariates(): Promise<number[][]> {
    throw new WeatherNotImplementedError(
      "the indus api is not public yet. implement this provider against it and return [feature][day].",
    );
  }
}
