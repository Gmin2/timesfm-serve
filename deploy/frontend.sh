#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/../frontend"
if [[ ! -f .vercel/project.json ]]; then
  printf '%s\n' 'First run: vercel link --yes --project forecast-lab --scope min2whos-projects'
  exit 1
fi

# Local builds include the verified holdout outside frontend/; only output is uploaded.
unset WEATHER_API_KEY_FILE WEATHER_ACCOUNT_ORIGIN WEATHER_PLAYGROUND_ORIGIN WEATHER_API_ORIGIN
vercel pull --yes --environment=production
vercel build --prod
vercel deploy --prebuilt --prod --archive=tgz
