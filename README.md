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
| `iex/` | power price forecasting |
| `timesfm_serve/`, `scripts/weather_*` | the weather platform and its experiments |
| `infra/`, `deploy/` | AWS and Kubernetes for the weather service |
| `tests/iex/`, `tests/` | tests for each |

the two applications share the repo and the python environment, nothing else.
neither imports from the other.

## tests

```bash
docker compose exec db createdb -U tfm tfm_test
DATABASE_URL=postgresql://tfm:tfm@localhost:15432/tfm_test uv run --no-sync pytest -q
```

sql and concurrency tests need real postgres. the IEX tests do not.

## limits

research, not a production service. the weather AWS resources cost money with
nothing shutting them down. TimesFM 3.0 weights are non-commercial, which is fine
here and would not be for a paid product. check the license before any commercial use.
