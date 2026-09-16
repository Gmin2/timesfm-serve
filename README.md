# timesfm-serve

forecasting with TimesFM, and checking honestly whether it is any good. two
applications share one repo.

**[power prices](#power-prices-iex), current work.** forecasting what
electricity will cost tomorrow on Indias power exchange, benchmarked against the
methods the field already uses.

**[weather](#weather-prior-work), prior work.** a deployed, metered forecasting
API on AWS: temperature for three Indian airports, with a Postgres queue, a CUDA
worker and a public dashboard.

## power prices (IEX)

indias power exchange sets a price for every 15 minutes of the next day, 96 of
them, announced around 1pm the day before. we forecast those 96 numbers before
they are announced.

the point is the comparison. TimesFM 3.0 has to beat a naive guess, and then beat
LEAR, the standard method in electricity price forecasting. nobody has published
that comparison for india.

**one rule above all others.** a forecast for a delivery day may only use
information that existed before **09:30 IST the day before**, when bidding opens.
that includes weather: only forecasts issued before the cutoff, never what the
weather turned out to be. leakage is the usual way price forecasting projects
fool themselves, so every slice keeps a leakage check green.

```bash
# one day, naive vs TimesFM 3.0
HF_HUB_CACHE=<hf cache> .venv/bin/python -m iex.tracer --day 2024-11-12

# pull every 15-minute price, four markets, about 8 minutes
.venv/bin/python -m iex.data --from 2022-01-01

# check the store against IEXs own aggregates and Grid-Indias copy
.venv/bin/python -m iex.verify
```

618k rows across DAM, RTM, GDAM and HP-DAM, 2022 to now. verified three ways: the
96 blocks reproduce IEXs published daily and hourly figures to under a paisa, the
observed maximum matches each CERC price cap on the exact day it took effect
(Rs 20, then Rs 12 on 3 Apr 2022, then Rs 10 on 4 Apr 2023), and Grid-Indias independent
copy agrees on 672 of 672 DAM blocks in a sample week.

data lives in `data/iex/`, gitignored: the IEX terms allow personal,
non-commercial use, so raw prices stay local and only results get published.

### what actually drives the price

price follows what the grid has to cover: demand, minus whatever wind and solar
turn up for free. grid-india publishes all three at the same 15 minutes the
exchange settles on, in the daily PSP report, from nov 2024.

```bash
# demand, wind, solar at 96 blocks a day
.venv/bin/python -m iex.drivers --from 2024-11-04

# forecast them two days out, which is as close as the cutoff allows
.venv/bin/python -m iex.driverforecast --from 2025-02-01
```

the PSP report for a day is published the morning after, so at the 09:30 cutoff
the newest one covers D-2, not D-1. the delivery day is two days out, so D-1 and
D both have to be forecast, and both halves come from one 192 block run.

on 483 sealed days, against the frozen headline, every model declared before the
run:

| model | mae | vs headline |
| --- | --- | --- |
| calendar + perfect drivers, a ceiling nobody can reach | 535.9 | -6.4% |
| calendar + drivers, wind from wind-site wind speed | 548.6 | **-4.2%** |
| calendar + drivers | 552.9 | -3.4% |
| calendar + weather + drivers | 554.1 | -3.2% |
| calendar + weather, the frozen headline | 572.4 | - |
| copy yesterday | 668.0 | +16.7% |

drivers beat the headline at p = 0.001. three things come out of it.

**drivers replace weather, they do not add to it.** weather plus drivers is no
better than drivers alone. demand, wind and solar are the channel weather uses to
reach the price, so once you forecast them the temperature has nothing left to
say. that also explains why the perfect-weather ceiling was worth nothing.

**we got about half the ceiling, then went after the other half.** the gap was
wind, at 22% error and barely better than copying yesterday. the cause was not the
model, it was where we were measuring: the weather covariates sample eight demand
centres, and we were giving east india 11% weight for wind, where wind generation
is zero.

weighting instead by grid-indias own regional wind generation over 678 days, and
cubing wind speed per site because turbine power goes as v cubed, wind speed at
the real wind sites correlates 0.956 with national wind generation against 0.370
for the demand cities.

```bash
# wind speed at seven wind sites, weighted by where the generation actually is
.venv/bin/python -m iex.weather --group wind --from 2024-11-01
.venv/bin/python -m iex.driverforecast --from 2025-02-01 --weather
```

| wind forecast, two days ahead | mae | vs persistence |
| --- | --- | --- |
| from its own history | 2633.6 | 10.5% better |
| with wind-site wind speed | **1665.2** | **43.4% better** |

demand, solar and net demand come out bit identical, which is the control that the
comparison is like for like.

that 36.8% better wind forecast is worth **0.78%** on the price, p = 0.015, and it
closes 25% of the remaining distance to the ceiling. the ratio is about 47 to 1,
because wind is 12 GW in a 200 GW system. the driver channel is now nearly spent:
12.7 mae of headroom left, and that is the impossible version.

**it does not explain the gap to the best commercial forecast.** run on the exact
88 day window pravah report a 498 mae over, drivers take us from 715.9 to 671.8,
worth 6.2% here rather than 3.4%, because drivers matter most when the grid is
tight. but that closes only 15% of their lead, and perfect drivers close 35%. two
thirds of the difference is neither weather nor generation nor demand.

| explanation | how it was tested | verdict |
| --- | --- | --- |
| better weather | perfect-weather ceiling | worth 0%, p = 0.21 |
| generation and demand | perfect-driver ceiling | worth 6.4%, a third of the gap |
| a better wind forecast | 37% better wind, measured | worth 0.78%, p = 0.015 |
| the bid stack | not tested yet | where the rest has to be |

the ceilings are the point. a negative result from a forecast you built could just
mean your forecast is bad. a negative result from the perfect version of that
input is a statement about the input. (their 498 is read off a public page for a
window they chose, so treat that row as indicative, not controlled.)

### a demand forecast that ties the system operator

grid-india publishes its own day-ahead demand error every day under IEGC 31.2(i),
which makes a rare like-for-like benchmark. 211 days, jan to sep 2026:

| forecaster | day-ahead mape |
| --- | --- |
| grid-india, who run the dispatch | 2.53% |
| ours, timesfm zero-shot | 2.50% |

a tie, and their figures are rounded to a tenth of a point so nobody wins. the
interesting part is the handicap: theirs is issued with live telemetry, ours is
issued a day further out from their own published actuals, with nothing trained.

## weather (prior work)

[dashboard](https://timesfms.vercel.app) ·
[api docs](https://88novucbtj.execute-api.us-east-1.amazonaws.com/docs) ·
[deployment](deploy/README.md) ·
[infrastructure](infra/README.md)

![system design: dashboard and API Gateway in front of a private load balancer, FastAPI and the ingestor on standard Kubernetes nodes, a TimesFM worker on a single GPU node, PostgreSQL, S3, Secrets Manager and CloudWatch](assets/system-design.png)

hourly temperature for three Indian airports: take ECMWF guidance, post-process it
with TimesFM conditioned on real station observations, serve the result through a
metered API.

fresh holdout, 4271 matched hours, 90 accepted cases out of 99 scheduled.

| method | test rmse |
| --- | --- |
| raw ECMWF | 1.718 C |
| TimesFM + ECMWF | 1.366 C |
| ridge correction | 1.359 C |

20.5% better than raw ECMWF, 95% interval [-0.485, -0.213] C. ridge basically ties it.
three airports over sampled origins, so it proves nothing about India as a whole, and
the quantiles are not calibrated.

PostgreSQL is the queue, the lease table and the result store. no broker. a separate
CUDA worker rehashes every input before it runs the model, so a replay either
reproduces its saved reference exactly or it fails. live forecasts expire after
8 hours and never fall back to a replay.

```bash
uv sync --frozen
docker compose up -d --build --wait
export DATABASE_URL=postgresql://tfm:tfm@localhost:15432/tfm
export WEATHER_API_KEY="$(uv run --no-sync python -m scripts.create_key weather-demo replay-demo)"
curl --fail -H "x-api-key: $WEATHER_API_KEY" http://127.0.0.1:18001/v1/weather/stations
```

docs at `http://127.0.0.1:18001/docs`. queued forecasts also need a gpu worker, one per
gpu, cuda on AWS or mps on a mac:

```bash
.venv/bin/python -m timesfm_serve.weather_worker --device mps --cache-dir <hf cache>
```

## layout

| path | what |
| --- | --- |
| `iex/` | power price forecasting, and its half of the API |
| `timesfm_serve/`, `scripts/weather_*` | the weather platform and its experiments |
| `timesfm_serve/api.py` | the gateway both are served behind |
| `infra/`, `deploy/` | AWS and Kubernetes |
| `tests/iex/`, `tests/` | tests for each |

one app, one database, one cluster, two applications. `/v1/weather` and `/v1/iex`
share authentication, api keys, rate limiting and health probes, and nothing else.
the forecasting code on each side never imports the other; they meet in
`timesfm_serve/api.py` and nowhere else, which is the only place sharing pays.

## tests

```bash
docker compose exec db createdb -U tfm tfm_test
DATABASE_URL=postgresql://tfm:tfm@localhost:15432/tfm_test uv run --no-sync pytest -q
```

sql and concurrency tests need real postgres, and so do the serving tests for
both applications. the IEX forecasting and backtest tests do not.

## limits

research, not a production service. the weather AWS resources cost money with
nothing shutting them down. TimesFM 3.0 weights are non-commercial, which is fine
here and would not be for a paid product. check the license before any commercial use.
