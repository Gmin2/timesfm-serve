# Forecast Lab

A React/TypeScript weather dashboard for the existing FastAPI service.
Uses selected Evilcharts components, Recharts, and restrained, reduced-motion-aware
transitions. The visual system (warm neutrals, one blue accent, hairline cards, status
pills) is documented in `design/README.md` with the reference screenshots in `design/inspo/`.

## Run

Requires Node.js 24, pnpm 11.5 and Python 3.12+. From this directory:

```bash
pnpm install --frozen-lockfile
pnpm dev
```

Open http://127.0.0.1:5178. The default is the historical archive; it works without
AWS credentials, PostgreSQL, or a model worker.

For live reads through the existing API, point the development server at an
operator-provided key file (never a `VITE_*` variable):

```bash
WEATHER_API_KEY_FILE=/absolute/path/to/api-key pnpm dev
```

`WEATHER_API_ORIGIN` optionally overrides the existing AWS API URL. The local server
binds to loopback and forwards only GET station/status/latest routes. The key is
read server-side and is not included in static assets. No replay jobs or inference
are triggered by browsing this dashboard.

## GitHub Login And API Keys

`/api-keys` provides GitHub sign-in, named API keys, one-time secret display,
revocation, sign-out, and the inline API playground with cURL/Python examples. Keys are
created in a side sheet (detail, review, secret key); the secret lives only in that
sheet's state and is dropped when it closes. All icons come from the local Nucleo
library, generated into `src/components/icons.tsx`.

Register a GitHub OAuth app with homepage `http://127.0.0.1:5178` and callback
`http://127.0.0.1:5178/api/auth/github/callback` for local development. Use a
separate OAuth app with your exact HTTPS dashboard origin for production.
See [GitHub's OAuth flow](https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/authorizing-oauth-apps).

Run the backend from the repository root with a dedicated local PostgreSQL
database and the client secret in an ignored file outside the frontend:

```bash
DATABASE_URL=postgresql://tfm:tfm@127.0.0.1:5432/tfm_dashboard \
GITHUB_CLIENT_ID=your-client-id \
GITHUB_CLIENT_SECRET_FILE=/absolute/path/to/github-client-secret \
WEATHER_DASHBOARD_ORIGIN=http://127.0.0.1:5178 \
WEATHER_PUBLIC_API_ORIGIN=http://127.0.0.1:18002 \
.venv/bin/uvicorn timesfm_serve.weather_api:app --host 127.0.0.1 --port 18002 --no-access-log
```

Then run the frontend in this directory:

```bash
WEATHER_ACCOUNT_ORIGIN=http://127.0.0.1:18002 pnpm dev
```

Existing live weather reads can still use `WEATHER_API_KEY_FILE` alongside that
setting. Keys issued by a **local** database work only against the local API;
they are not AWS keys. Examples use the backend's configured public API origin.
Set `WEATHER_API_KEY` privately in your shell to the generated key before running
the examples; the snippets never embed that secret.

- Authlib handles authorization-code + S256 PKCE. OAuth state is browser-bound,
  single-use and expires in 10 minutes. Provider tokens are discarded after the
  authenticated profile is fetched. No repository/email scopes are requested.
- Accounts link to GitHub's numeric ID, not a mutable login/email.
- Session cookies are HttpOnly, SameSite=Lax, Secure with a `__Host-` prefix on
  HTTPS. Only hashes are stored. Sessions expire after seven days; at most five
  are retained per account. Logout deletes the session.
- Key mutations require the configured Origin and a session-bound CSRF token.
  Only key hashes are stored. The secret is returned once and is never retained
  in browser storage or the React Query cache.
- Self-service keys have `weather:read` access, three active keys per account,
  twenty creations per day, and the account's per-key rate (default 60/minute).
  They cannot enqueue replays. GitHub accounts receive zero replay credits.
  Operator-issued keys retain their existing capabilities.
- Missing OAuth configuration disables sign-in. No fake login is offered.

Auth routes are in `timesfm_serve/customer_auth.py`, storage is in
`timesfm_serve/customer_store.py`, and migration `004_dashboard_auth.sql` adds
persistent state. Reprovision the production API database role with the updated
operator migration job before releasing this API image.

## Navigation And Playground

The app has real routes: `/forecasts`, `/benchmarks`, `/runs`, and `/api-keys`.
The playground lives below key management on `/api-keys`; old `/playground` links
redirect to `/api-keys#playground`. Links support direct entry, refresh, new tabs and browser history.
Old `/?view=...` bookmarks redirect to the corresponding path. Forecast filters
such as station, run and timezone still use query parameters.

The playground sends read-only requests using the pasted key, not a displayed key
prefix. Existing secrets cannot be retrieved from the server. Key input and
responses clear on sign-out or revocation and never enter browser storage.
It supports the station list, latest forecast, station status and replay catalog,
and shows the real response body, selected response headers, HTTP status, elapsed
time, size and remaining rate limit. Requests can be cancelled; JSON responses
can be copied or downloaded. cURL/Python snippets use an environment-variable
placeholder instead of the entered secret.

`WEATHER_PLAYGROUND_ORIGIN` selects the fixed backend destination. It defaults
to `WEATHER_API_ORIGIN`, then the existing AWS API, independently of the account
backend. To use a local API explicitly, launch Vite with
`WEATHER_PLAYGROUND_ORIGIN=http://127.0.0.1:18002`.
The base URL is visible in the playground. Local keys work only against
the local database/API, not AWS. Keys remain in component memory and are
discarded on leaving the page; the proxy never substitutes an operator key or
forwards browser cookies. It allows only the listed GET routes, blocks redirects,
times out after 15 seconds and caps responses at 1 MiB.

## Data

`pnpm dev` and `pnpm build` run
`../scripts/weather_dashboard_data.py`. This uses the checked-in
`results/weather/india_station_seasonal_v1/holdout` artifacts and creates ignored
`public/data/` JSON. Case prediction checksums, consecutive hourly timestamps,
quantile ordering and case counts are checked before export.

- Historical: 90 scored cases, 99 scheduled runs, 4,271 matched observations.
  This is a view of saved holdout outputs, not a new GPU replay.
- Live: the authenticated API's issued forecast and raw ECMWF guidance.
  Stale/unavailable responses never fall back to the historical archive.
- Observations, Ridge, and scores are absent in live mode until prospective
  verification is implemented. No synthetic values are substituted.
- Quantiles are uncalibrated; benchmarks are retrospective three-station results.
- IST/UTC formatting affects display only. Downloaded timestamps retain their
  explicit timezone; missing observation cells remain empty.

## Verification

```bash
pnpm test
pnpm lint
pnpm build
pnpm exec playwright install chromium
pnpm test:e2e
```

Browser tests expect the dev server to be running. Override the URL with
`DASHBOARD_TEST_URL`; optionally provide a preinstalled Chromium binary via
`PLAYWRIGHT_CHROMIUM_EXECUTABLE`. API failures in automated browser tests are
controlled fixtures; actual AWS reads are a separate smoke check.

The root Python tests include `tests/test_weather_dashboard_data.py`, which
recalculates the exported TimesFM RMSE and checks missing-observation coverage.

## Files

- `design/`: design system notes and inspiration screenshots.
- `src/App.tsx`: app shell, navigation, station/run selection and query lifecycle.
- `src/components/icons.tsx`: Nucleo icons as React components.
- `src/components/run-archive.tsx`: scheduled run table with coverage meters.
- `src/components/forecast-panel.tsx`: forecast controls, table and downloads.
- `src/components/forecast-chart.tsx`: time-scaled curves and p10-p90 range.
- `src/components/benchmarks.tsx`: measured model/station/lead comparisons.
- `src/lib/weather.ts`: data adapters, validation, timestamps and scoring.
- `src/registry/ui/chart.tsx`: selected Evilcharts chart foundation.
- `server/api-proxy.ts`: local development/preview API adapter.
- `api/gateway.ts`: production Vercel gateway for account and read-only API routes.
- `vercel.json`: production runtime, headers and explicit SPA/API routing.
- `../deploy/frontend.sh`: local build and prebuilt Vercel deployment.
- `e2e/`: browser tests across desktop, tablet and mobile.
- `licenses/`: component attribution and retained notices.

## Vercel Deployment

Public dashboard: https://timesfms.vercel.app

The historical dashboard, benchmark, run archive and API playground are deployed
on Vercel. FastAPI, PostgreSQL, ingestion and the GPU remain on AWS. The playground
uses the visitor's entered AWS API key; it never substitutes an operator key.
GitHub sign-in and self-service read-only keys use the deployed AWS account
endpoints. Anonymous live dashboard reads remain disabled until a dedicated
read-only dashboard credential is provisioned.

Run `bash deploy/frontend.sh` from the repository root to redeploy. It builds
locally, including the verified holdout outside `frontend/`, then uploads only
Vercel's prebuilt output. Do not deploy the repository root or use a frontend-only
remote build without its parent data artifacts. Git auto-deployment is not set up.
Vercel's local project metadata and environment files are Git-ignored.

`api/gateway.ts` runs on Node.js 24 in `iad1`, alongside static assets on Vercel's
CDN. `vercel.json` serves `index.html` for the UI routes and legacy playground link while API, asset and
data failures keep their real status codes. Vite middleware is local-only and is
not instantiated during production builds.

Optional Vercel **server-side** production variables:

- `WEATHER_API_ORIGIN`: existing AWS API by default.
- `WEATHER_PLAYGROUND_ORIGIN`: API origin by default, independent of account login.
- `WEATHER_ACCOUNT_ORIGIN`: set only after AWS exposes the account endpoints.
- `WEATHER_DASHBOARD_API_KEY`: a dedicated read-only, zero-credit key for public
  dashboard reads, never the operator key. Store as a sensitive Vercel variable.

No AWS credentials or GitHub client secret belong in Vercel or `VITE_*` variables.
The GitHub secret remains on the AWS backend. The production OAuth homepage is
`https://timesfms.vercel.app`, and its callback is
`https://timesfms.vercel.app/api/auth/github/callback`.

The account gateway preserves cookies, Origin, CSRF headers, HTTP status and
redirects without caching, and never attaches a dashboard/operator key. All
upstreams must be fixed HTTPS origins; playground requests are allowlisted GETs,
with a 15-second upstream timeout and 1 MiB response limit. See `deploy/README.md`
for AWS account release and rollback steps.

The UI does not implement replay submission or prospective accuracy collection.
The forecasting engine and GPU worker remain unchanged.
