# timesfm-serve

hourly temperature forecasts for three Indian airports. takes ECMWF guidance, post
processes it with TimesFM conditioned on real station observations, serves the result
through a metered API.

[dashboard](https://timesfms.vercel.app) ·
[api docs](https://88novucbtj.execute-api.us-east-1.amazonaws.com/docs) ·
[deployment](deploy/README.md) ·
[infrastructure](infra/README.md)

![system design: dashboard and API Gateway in front of a private load balancer, FastAPI and the ingestor on standard Kubernetes nodes, a TimesFM worker on a single GPU node, PostgreSQL, S3, Secrets Manager and CloudWatch](assets/system-design.png)

## accuracy

fresh holdout, 4271 matched hours, 90 accepted cases out of 99 scheduled.

| method | test rmse |
| --- | --- |
| raw ECMWF | 1.718 C |
| TimesFM + ECMWF | 1.366 C |
| ridge correction | 1.359 C |

20.5% better than raw ECMWF, 95% interval [-0.485, -0.213] C. ridge basically ties it.
three airports over sampled origins, so it proves nothing about India as a whole, and
the quantiles are not calibrated.

## how it works

PostgreSQL is the queue, the lease table and the result store. no broker. a separate
CUDA worker rehashes every input before it runs the model, so a replay either
reproduces its saved reference exactly or it fails. live forecasts expire after
8 hours and never fall back to a replay.

## run it

needs docker compose, python 3.12+ and uv.

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

tests need a throwaway database:

```bash
docker compose exec db createdb -U tfm tfm_test
DATABASE_URL=postgresql://tfm:tfm@localhost:15432/tfm_test uv run --no-sync pytest -q
```

## limits

a demo of forecasting infrastructure, not a production service. prospective accuracy
and operational hardening are still open, and the AWS resources cost money with
nothing shutting them down. check the pinned TimesFM license before commercial use.
