# timesfm-serve

a forecast serving layer for indian grid demand, built around google's timesfm 3.
you send demand history plus weather covariates, you get back a quantile forecast
over http. sync for single series, async batch jobs for many.

the model is a stand in. the weather covariate slot is designed for a real
weather forecast provider, and the serving layer is what an indus style model
would need to be exposed as an api.

## does weather help

14 day ahead daily peak demand, weekly forecast origins from april 2023 to
april 2024 (53 origins), 512 days of context, five states. data is grid india
daily peak met (via zenodo 14983362) and era5 daily weather for the same states.

| model | mean mape |
|---|---|
| seasonal naive (same weekday last week) | 8.26 % |
| timesfm 3, demand only | 6.24 % |
| timesfm 3 + weather covariates | 4.55 % |

per state numbers and crps are in `results/eval_14d_weekly_2023.csv`.
reproduce with `python scripts/eval.py`.

caveat: the covariates are actual era5 weather for the forecast days, not a
weather forecast. so this is the upper bound a perfect weather model would give.
the gap between the two timesfm rows is the value of a good weather forecast.

## api

```
GET  /health
POST /forecast        sync, one series           x-api-key required
POST /jobs            async batch, up to 5000    x-api-key required
GET  /jobs/{id}       status + results           x-api-key required
```

forecast request:

```json
{
  "series": [14728.0, 15242.0, "..."],
  "horizon": 14,
  "future_covariates": [[...temp_mean...], [...temp_max...], [...ghi...], [...wind...]]
}
```

future covariates are shaped `[n_features][len(series) + horizon]`. past only
covariates are `[n_features][len(series)]`. response carries the point forecast,
nine quantiles (p10 to p90) per step and the model id.

## run it

```
docker compose up -d
docker compose exec api python scripts/create_key.py demo
curl -X POST localhost:8000/forecast -H "x-api-key: $KEY" -H 'content-type: application/json' -d @req.json
```

the checkpoint is baked into the image at build time, so containers start in
seconds and never talk to the hub at runtime. `--build-arg TORCH=gpu` builds
the cuda variant for the gpu node, the default cpu image is about 4 gb, 1.4 of which is the checkpoint.

## kubernetes

`k8s/base` is a kustomize base: postgres statefulset, redis, api deployment
with http probes, worker deployment with a readiness probe that only passes
once the model is warm, an hpa on api cpu, and a keda scaledobject that scales
the worker on redis queue depth, down to zero when idle.

```
kind create cluster --name tfm
kind load docker-image pravah-api:latest --name tfm
kubectl apply --server-side -f https://github.com/kedacore/keda/releases/download/v2.17.2/keda-2.17.2.yaml
kubectl apply -k k8s/overlays/kind
kubectl -n tfm port-forward svc/api 8080:80
```

`k8s/overlays/eks` pins the worker to a tainted gpu node group and requests
`nvidia.com/gpu: 1`. `k8s/eks-cluster.yaml` is the eksctl config with a spot
g5.xlarge group that scales to zero. `scripts/create_eks.sh` then
`scripts/deploy_eks.sh` do the whole thing.

## observability

`GET /metrics` exposes prometheus metrics: request counts and latency by
route and status, model predict latency, series forecast, queue depth and the
loaded model. every response carries an `x-response-time-ms` header and every
sync forecast is logged to the `forecast_runs` table with tenant and latency.

## layout

```
timesfm_serve/api.py      fastapi app, sync forecast, job submit and poll
timesfm_serve/jobs.py     rq task that runs predict_batch on a warmed model
timesfm_serve/worker.py   worker entry point
timesfm_serve/db.py       postgres: api keys, run log, jobs, job results
timesfm_serve/metrics.py  prometheus counters and histograms
k8s/                      kustomize base plus kind and eks overlays
timesfm_serve/data.py     demand + weather loader used by the eval
scripts/eval.py           rolling origin benchmark
scripts/smoke.py          load the model and forecast a toy series
```

## license note

timesfm 3 weights are under google's non commercial license. this repo is a
demo of the serving layer, not a product. swap the model for anything with the
same predict interface.
