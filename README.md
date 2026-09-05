# timesfm-serve

a forecasting api for indian grid demand. you send demand history and a
location, it fetches the weather itself, and returns a quantile forecast.

the model is google's timesfm 3, used as a stand in. the point of the project
is the serving layer around it: the thing a weather or grid model needs before
anyone outside your company can call it.

## does a weather forecast actually help

7 day ahead daily peak demand, weekly forecast origins from april 2023 to
april 2024, 512 days of context, six states. demand is grid india daily peak
met via zenodo 14983362. weather is open-meteo: the reanalysis archive for what
the weather was, and the previous-runs archive for what the forecast said at
the correct lead time.

| method | mean mape |
|---|---|
| seasonal naive, same weekday last week | 7.82 % |
| gradient boosted trees on demand lags, temperature and calendar | 7.43 % |
| timesfm 3, demand only | 5.57 % |
| **timesfm 3 + the temperature forecast that existed on the day** | **5.03 %** |
| timesfm 3 + perfect temperature | 4.46 % |
| timesfm 3 + perfect temperature, irradiance and wind | 4.25 % |

the fourth row is what an operator would actually get. the fifth is the ceiling
a perfect temperature forecast would reach. **today's forecast captures about
half of the benefit that perfect weather would give, and the missing half is
what a better weather model is worth.** that gap is the entire argument for
something like indus, and it is measured here rather than asserted.

per state numbers, crps and calibration are in
`results/eval_7d_weekly_2023.csv`. reproduce with `uv run python scripts/eval.py`
(add `--device mps` on an apple laptop, it is about 2x).

### why the horizon is 7 days and the covariate is temperature

both are constraints, stated rather than hidden.

open-meteo's previous-runs archive carries lead times back about a week, so a
14 day forecast cannot be evaluated with lead-correct weather. and for these
locations it carries **temperature only** — radiation and wind come back
completely empty. so the honest comparison holds the variable set fixed at
temperature and varies only foresight. the last row shows what perfect
radiation and wind would add on top.

### three things that made the first version of this wrong

worth writing down, because each one produced a confident number that was not
true:

1. **covariates were reanalysis actuals**, which is perfect hindsight weather.
   that reported a 4.55 % headline at 14 days. it is a ceiling, not a result.
2. **missing hours aggregated to zero.** the forecast archive returns a row for
   every hour and leaves most variables null. `resample().sum()` treats null as
   zero, which manufactured a 76 % low bias in irradiance that looked like a
   finding. days are now dropped unless 20 of 24 hours are present.
3. **the two archives disagree systematically.** the forecast model and the
   reanalysis are different models on different grids. delhi's day-ahead
   temperature sits about 4 °C above the reanalysis, which is far too large to
   be forecast error, and scoring it as one made delhi's demand error double.
   the forecast is now bias corrected against a trailing 60 day window, shifted
   so it only ever uses what was known before the forecast was made. residual
   bias is under 0.15 °C everywhere and lead 7 rmse is 1.0 to 2.0 °C, which is
   what a real 7 day temperature forecast looks like.

calibration is checked too: the p10 to p90 band should contain 80 % of actuals
and lands between 71 % and 81 % across states, so the intervals are slightly
narrow but broadly honest.

## shape

one fastapi app with the model loaded in process. no gateway, no sidecar, no
network hop to reach the model.

```
timesfm_serve/api.py      the api, loads timesfm at startup
timesfm_serve/weather.py  weather providers, open-meteo and an indus slot
timesfm_serve/jobs.py     batch forecasting, run on a redis queue
timesfm_serve/worker.py   the queue worker
timesfm_serve/auth.py     hashed api keys and per key rate limiting
timesfm_serve/oauth.py    github sign in
timesfm_serve/dashboard.py  account page: credits, create and revoke keys
timesfm_serve/db.py       postgres: accounts, keys, credits, jobs, sessions
timesfm_serve/demo.py     the demo page and its data
migrations/               plain sql, applied in order behind an advisory lock
scripts/covariates.py     lead-correct forecast weather, bias corrected
scripts/eval.py           the benchmark that produced the table above
```

running it is `docker compose up`: the api, a queue worker, postgres and redis.

the demo page covers six states: karnataka, gujarat, delhi, maharashtra, tamil
nadu and assam. assam is deliberately included as the small wet outlier, about
a seventh of karnataka's peak, to check the approach is not tuned to one state.

## api

```
POST /forecast           series + optional covariates -> quantile forecast
POST /forecast/weather   series + lat/lon -> the service fetches the weather
POST /jobs               batch, up to 5000 series, returns a job id
GET  /jobs/{id}          status and results
GET  /usage              credits granted, used, remaining
GET  /weather/providers  which providers are wired up
GET  /metrics            prometheus
```

all take `x-api-key`. every response carries `x-request-id`,
`x-response-time-ms`, `x-ratelimit-*` and `x-credits-remaining`.

```bash
curl -X POST localhost:8000/forecast/weather \
  -H "x-api-key: $KEY" -H 'content-type: application/json' \
  -d '{"series": [/* daily demand */], "last_date": "2024-03-01",
       "lat": 12.97, "lon": 77.59, "horizon": 14}'
```

## accounts, keys and credits

sign in with github at `/dashboard` and you get an account with 50,000 free
credits and somewhere to mint and revoke api keys. keys are stored as sha256
with a short prefix kept for identification, so a database leak does not leak
anyone's credentials, and the key itself is shown exactly once.

**one credit is one forecast step**, so a 14 day forecast costs 14 and a batch
of 500 series costs `500 x horizon`. requests are not the unit because one
request can be a thousand times more work than another.

credits are a balance on the account rather than a monthly window, and they are
reserved in a single atomic statement **before** the model runs, then refunded
if it fails:

```sql
update accounts set credits_used = credits_used + %s
where id = %s and suspended_at is null
  and credits_used + %s <= credits_granted
returning credits_granted - credits_used
```

no row back means out of credits, which is a `402`. that ordering is the part
worth getting right: count usage afterwards and ten concurrent requests all
pass the check and blow through the cap. there is a test that fires twenty
concurrent reservations at a hundred credit grant and asserts exactly ten
succeed.

rate limits are a separate thing and live on the key, not the account, because
they protect the service rather than the bill. a user rotating keys should not
have their balance reset. the limiter runs before the reservation, so a
throttled call is never charged.

`forecast_runs` is an append only ledger. every credit spent has a row with the
account, points, latency and request id, so a balance that looks wrong can be
recomputed and traced back to the calls behind it.

## weather providers

`timesfm_serve/weather.py` holds the provider protocol, an open-meteo
implementation that stitches the archive and forecast apis, and `Indus`, which
is pravah's model. that one raises `NotImplementedError` and the endpoint turns
it into a `501` carrying the shape it needs to return. wiring a real weather
model in is implementing one method.

## batch jobs

a forecast for one feeder is a http call. a forecast for every feeder in a
state is a job. `POST /jobs` queues the batch, a worker with the model already
warm runs `predict_batch` over it, and results land in postgres.

the worker runs jobs in process rather than forking, because forking after
torch has spun up its thread pool deadlocks on linux. that cost an afternoon to
find, so it is worth writing down.

## running it

```bash
docker compose up -d
docker compose exec api python scripts/create_key.py demo
open http://localhost:8000
```

the checkpoint is baked into the image at build time and `HF_HUB_OFFLINE=1` is
set, so containers start without touching the network. `--build-arg TORCH=gpu`
builds the cuda variant; the default cpu image is about 4 gb, 1.4 of which is
the checkpoint.

## tests

```bash
uv run pytest
```

24 tests against a real postgres rather than a mock, because the parts worth
testing are the sql: that concurrent reservations cannot oversell a grant, that
a failed forecast refunds, that a revoked key stops working. ci runs them
against a postgres service container.

## history worth knowing

this ran three other ways before settling here, and the reasons are more useful
than the code:

- **kubernetes**, with keda scaling the batch worker from zero on queue depth.
  verified in kind. replaced because the operational weight was not earning
  anything at this size.
- **a typescript gateway in front of a python inference service**, deployed on
  aws as a lambda plus an app runner container. it worked, but it was two
  services and two languages for a product that fits in one, so it was folded
  back into this app and everything it had — accounts, credits, hashed keys,
  rate limits, github sign in — was ported to python. the branch is
  `typescript-gateway` if you want to see it.
- **the model on aws lambda**, which is the one worth remembering. measured
  there it spent **95 seconds** importing torch and loading weights on every
  cold start, ran the forecast itself in 3.9 seconds, and used **2986 mb of a
  3008 mb ceiling**. a model server wants to load once and stay resident, which
  is exactly what lambda refuses to let it do.

## license note

timesfm 3 weights are under google's non commercial license, so this is a
demonstration of the serving layer, not a product.
