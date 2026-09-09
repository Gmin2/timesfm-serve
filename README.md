# timesfm-serve

hourly temperature forecasts for three Indian airports. it takes ECMWF guidance,
post processes it with TimesFM conditioned on real station observations, and serves
the result through a metered, key authenticated API.

[dashboard](https://timesfms.vercel.app) ·
[api docs](https://88novucbtj.execute-api.us-east-1.amazonaws.com/docs) ·
[deployment](deploy/README.md) ·
[infrastructure](infra/README.md)

![system design: dashboard and API Gateway in front of a private load balancer, FastAPI and the ingestor on standard Kubernetes nodes, a TimesFM worker on a single GPU node, PostgreSQL, S3, Secrets Manager and CloudWatch](assets/system-design.png)

## what it does

- 48 hour forecasts at Guwahati, Hyderabad and Chennai, on 6 hour origins
- PostgreSQL is the queue, the lease table and the result store. no broker
- a separate CUDA worker rehashes every input before it runs the model
- historical replays reproduce saved references exactly, or they fail loudly
- live forecasts expire after 8 hours and never fall back to a replay
- hourly scoring checks published forecasts against later observations, off the GPU

model inference needs CUDA on AWS, or MPS locally on a mac. theres no cpu fallback.

## accuracy

fresh holdout, 4271 matched hours from 90 accepted cases out of 99 scheduled.

| method | test rmse |
| --- | --- |
| raw ECMWF guidance | 1.718 C |
| TimesFM + ECMWF | 1.366 C |
| ridge correction | 1.359 C |

20.5% lower rmse than raw ECMWF, paired 95% interval [-0.485, -0.213] C.

ridge basically ties it. this is three airports over sampled origins, not proof of
beating ECMWF or anything else across India. quantiles are not calibrated, and the
live feed uses different sources so it needs its own validation.
scores and provenance are in the
[holdout manifest](results/weather/india_station_seasonal_v1/holdout/manifest.json).

## run it locally

needs docker compose, python 3.12+ and uv.

```bash
uv sync --frozen
docker compose up -d --build --wait
export DATABASE_URL=postgresql://tfm:tfm@localhost:15432/tfm
export WEATHER_API_KEY="$(uv run --no-sync python -m scripts.create_key weather-demo replay-demo)"
curl --fail -H "x-api-key: $WEATHER_API_KEY" http://127.0.0.1:18001/v1/weather/stations
```

docs are at `http://127.0.0.1:18001/docs`. compose only starts postgres and the
lightweight api. those credentials are for local dev, dont expose the ports.

queued forecasts need a gpu worker too. on a mac, in another terminal:

```bash
DATABASE_URL=postgresql://tfm:tfm@localhost:15432/tfm \
  .venv/bin/python -m timesfm_serve.weather_worker \
  --device mps --cache-dir /path/to/huggingface/hub
```

only run one worker per gpu. to verify a full replay end to end:

```bash
.venv/bin/python -m scripts.weather_api_smoke \
  --base-url http://127.0.0.1:18001 --verify-reference
```

## api

| method | route |
| --- | --- |
| GET | `/v1/weather/stations` |
| GET | `/v1/weather/stations/{station_id}/latest` |
| GET | `/v1/weather/replays` |
| POST | `/v1/weather/replays` |
| GET | `/v1/weather/jobs/{job_id}` |
| GET | `/v1/weather/forecasts/{job_id}` |
| GET | `/health/live`, `/health/ready` |

everything needs `x-api-key`. replay submission also needs `Idempotency-Key` and
costs 48 credits, reserved once per submission. the same key with a different case
returns 409. jobs and forecasts are owner only. github sign in issues read only keys.

## tests

```bash
docker compose exec db createdb -U tfm tfm_test
DATABASE_URL=postgresql://tfm:tfm@localhost:15432/tfm_test uv run --no-sync pytest -q
uv run --no-sync ruff check .
```

sql and concurrency tests run against real postgres. inference is mocked in the queue
tests, so a green suite does not prove a gpu replay works. thats the smoke check above.
ci also builds the non ml images and validates the terraform and kubernetes config.

## scope

this is a demo of forecasting infrastructure and api engineering, not a production
service. prospective accuracy, high availability and operational hardening are still
open. the aws resources cost money and nothing shuts them down automatically.

the old electricity demand demo is out of the working tree but still in git history at
`14002b05619c7894e3b65bb19c1970a3d06bd32a`. check the pinned TimesFM license before
any commercial use.
