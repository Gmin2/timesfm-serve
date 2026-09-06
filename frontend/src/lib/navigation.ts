import type { Mode, Zone } from './weather'

export const PAGE_PATHS = { forecasts: '/forecasts', benchmarks: '/benchmarks', runs: '/runs', access: '/api-keys' } as const
export type Page = keyof typeof PAGE_PATHS

export function legacyDestination(search: string): string {
  const previous = new URLSearchParams(search)
  const view = previous.get('view') || 'forecasts'
  if (view === 'playground') return '/api-keys#playground'
  const page = Object.hasOwn(PAGE_PATHS, view) ? view as Page : 'forecasts'
  const next = new URLSearchParams()
  for (const name of page === 'access' ? ['auth_error'] : ['station', 'run', 'mode', 'zone']) {
    const value = previous.get(name)
    if (value) next.set(name, value)
  }
  return PAGE_PATHS[page] + (next.size ? '?' + next.toString() : '')
}

export function forecastHref(station: string, zone: Zone, mode: Mode = 'historical_replay', run?: string): string {
  const params = new URLSearchParams()
  if (station !== '42410099999') params.set('station', station)
  if (zone === 'UTC') params.set('zone', 'UTC')
  if (mode === 'experimental_live') params.set('mode', 'live')
  else if (run) params.set('run', run)
  return '/forecasts' + (params.size ? '?' + params.toString() : '')
}
