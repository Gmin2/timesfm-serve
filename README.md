# timesfm-serve

a forecasting api for indian grid demand. you send demand history and a
location, it fetches the weather itself, and returns a quantile forecast.

the model is google's timesfm 3, used as a stand in. the point of the project
is the serving layer around it: the thing a weather or grid model needs before
anyone outside your company can call it.

## does weather actually help

14 day ahead daily peak demand, weekly forecast origins from april 2023 to
april 2024 (53 origins), 512 days of context, five states. demand is grid india
daily peak met via zenodo 14983362, weather is era5 for the same states.

| method | mean mape |
|---|---|
| seasonal naive, same weekday last week | 8.26 % |
| timesfm 3, demand only | 6.24 % |
| timesfm 3 with weather covariates | 4.55 % |

per state numbers and crps are in `results/eval_14d_weekly_2023.csv`.
reproduce with `uv run python bench/eval.py`.

one caveat stated plainly: the covariates are actual era5 weather for the
forecast days, not a weather forecast. so 4.55 % is the ceiling a perfect
weather model would buy you. the gap between the last two rows is what a good
weather forecast is worth, which is the whole argument for a model like indus.

## shape

```
gateway/     typescript. the api, accounts, credits, keys, dashboard, demo page
inference/   python. ~90 lines. loads timesfm and answers /predict
bench/       python. the benchmark that produced the table above
```

the model only runs in pytorch, so it lives in its own container behind one
endpoint and nothing else. everything a person would actually change is
typescript.

`infra/` is the aws deployment, cdk in typescript.

## why the model is not on lambda

it was, first. the gateway and the model both ran as container lambdas. the
gateway is a good fit and stayed. the model was not, and the numbers are worth
writing down because they are the whole argument:

| | on lambda |
|---|---|
| import torch and load weights | 95 s, on every cold start |
| the forecast itself, once loaded | 3.9 s |
| memory used | 2986 MB of a 3008 MB ceiling |

lambda throws the loaded model away between invocations, so it pays that 95
seconds again and again, and it did it at 99% of the memory the account allows.
that is not a tuning problem, it is the execution model: a model server wants to
load once and stay resident.

so the split is the one production usually lands on. the api layer is
serverless, because it is stateless and boots in about a second. the model is
one always warm container on app runner, 1 vcpu and 3 gb, loaded once. about 20
usd a month, which is the honest price of not making every visitor wait.

two consequences worth knowing:

- app runner has no architecture setting and only accepts amd64, so the
  inference image is built for x86_64 while the gateway stays arm64.
- app runner's public url has no iam auth, so the gateway proves itself to the
  model with a shared secret from parameter store rather than sigv4.

## deploying

```bash
aws ssm put-parameter --name /timesfm-serve/BETTER_AUTH_SECRET --type SecureString --value "$(openssl rand -base64 32)"
aws ssm put-parameter --name /timesfm-serve/INFERENCE_API_KEY  --type SecureString --value "$(openssl rand -base64 32)"
cd infra && pnpm install && pnpm exec cdk bootstrap && pnpm deploy
```

the stack is a postgres instance, an app runner service for the model, and a
lambda behind a function url for the gateway. there is deliberately no nat
gateway: the lambda stays outside the vpc so it keeps internet access for the
weather api and github oauth, which a nat would otherwise cost about 32 usd a
month to restore. the trade is that the database is reachable from the internet,
so tls is forced, the password is generated into secrets manager, and the
gateway verifies the rds certificate against a bundled ca.

github sign in is optional. add `GITHUB_CLIENT_ID` and `GITHUB_CLIENT_SECRET`
to the same parameter store prefix and it turns itself on; missing parameters
are logged and skipped.

## api

```
POST /v1/forecast          series + optional covariates -> quantile forecast
POST /v1/forecast/weather  series + lat/lon -> the service fetches the weather
GET  /v1/usage             credits granted, used, remaining
GET  /v1/weather/providers which weather providers are wired up
```

all four take `x-api-key`. every response carries `x-request-id`,
`x-ratelimit-*` and `x-credits-remaining`.

```bash
curl -X POST localhost:3000/v1/forecast/weather \
  -H "x-api-key: $KEY" -H 'content-type: application/json' \
  -d '{"series": [/* daily demand */], "last_date": "2024-03-01",
       "lat": 12.97, "lon": 77.59, "horizon": 14}'
```

covariate lengths are checked before they reach the model: past covariates must
match the series length, future covariates must cover series plus horizon. the
model would silently pad or truncate instead, which hides a caller mistake and
quietly ruins the forecast.

## credits

one credit is one forecast step, so a 14 day forecast costs 14. requests are
not the unit because one request can be a thousand times more work than
another.

credits are a balance on the account, not a monthly window, and they are
reserved in a single atomic statement **before** the model runs, then refunded
if inference fails. that ordering matters: count usage afterwards and ten
concurrent requests all pass the check and blow through the cap. running out
returns `402`.

rate limits are separate and per key, because they protect the service rather
than the bill. a user rotates keys without their balance resetting.

## weather providers

`gateway/src/weather/` holds the provider interface, an open-meteo
implementation that stitches the archive and forecast apis, and `indus.ts`,
which is pravah's model. that one throws `501` with the shape it needs to
return. wiring a real weather model in is implementing one method.

## auth

github sign in via better auth. first sign in creates an account with 50,000
free credits. the dashboard at `/dashboard.html` shows the balance and lets you
create and revoke keys.

keys are stored as sha256 with a short prefix kept for identification, so a
database leak does not leak anyone's credentials. the key itself is shown once,
at creation.

## running it

```bash
docker compose up -d
open http://localhost:3000
```

for github sign in, copy `gateway/.env.example` to `gateway/.env`, create a
github oauth app with callback `http://localhost:3000/api/auth/callback/github`
and fill in the id and secret. without it everything except sign in works, and
you can mint a key directly:

```bash
cd gateway && pnpm key my-account "laptop"
```

development, with the services on the host:

```bash
cd gateway && pnpm install && pnpm dev        # :3000
uv run uvicorn inference.main:app --port 8100 # :8100
```

## tests

```bash
cd gateway && pnpm test   # 26 tests, needs postgres
uv run pytest             # the benchmark metrics
```

the gateway tests run against a real postgres rather than a mock, because the
parts worth testing are the sql: that concurrent reservations never oversell a
grant, that a failed forecast refunds, that a revoked key stops working. ci
runs them against a postgres service container.

## license note

timesfm 3 weights are under google's non commercial license, so this is a
demonstration of the serving layer, not a product. swapping the model out is
changing one container that exposes `/predict`.

an earlier version of this ran on kubernetes with keda scaling a batch worker
from zero. that is in the git history; the current target is serverless with
scale to zero.
