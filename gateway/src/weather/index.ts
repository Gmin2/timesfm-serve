import { Indus } from "./indus.js";
import { OpenMeteo } from "./open-meteo.js";
import type { WeatherProvider } from "./types.js";

const providers = new Map<string, WeatherProvider>([
  ["open-meteo", new OpenMeteo()],
  ["indus", new Indus()],
]);

export function getProvider(name: string): WeatherProvider | undefined {
  return providers.get(name);
}

export function providerNames() {
  return [...providers.keys()];
}

export * from "./types.js";
