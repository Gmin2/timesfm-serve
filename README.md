# timesfm-serve

An independent weather-forecasting API prototype: use station observations and
TimesFM to post-process ECMWF temperature guidance, then deliver stored forecasts
through an authenticated API.

The AWS pilot uses FastAPI, PostgreSQL, Kubernetes and a separate NVIDIA GPU worker.
[API docs](https://88novucbtj.execute-api.us-east-1.amazonaws.com/docs) |
[Deployment assets](deploy/README.md) |
[Infrastructure](infra/README.md)

## Dashboard

The [Forecast Lab frontend](frontend/README.md) provides station/run selection,
temperature comparisons, uncalibrated quantile ranges, CSV/JSON export, the
retrospective benchmark, and all 99 scheduled cases including rejections.
Historical views use verified saved artifacts; live views use server-side adapters
and never substitute historical forecasts.
The public dashboard is [live on Vercel](https://timesfms.vercel.app),
with historical views and an AWS-connected, bring-your-own-key API playground.
GitHub sign-in and self-service read-only API keys are enabled on the public API
access page. Credentials remain on AWS; Vercel proxies the account routes.
Anonymous live dashboard reads remain disabled until a dedicated read-only
dashboard credential is provisioned. See the frontend README for local setup.

```bash
cd frontend
pnpm install --frozen-lockfile
pnpm dev
```

Open http://127.0.0.1:5178 for local development.
See its README to enable authenticated live reads without putting a key in the
browser bundle.

## What It Does

- Forecasts hourly temperature for 48 hours at Guwahati, Hyderabad and Chennai.
- Historical replay uses frozen inputs and checks numerical agreement with saved references.
- Experimental live ingestion captures NOAA AWC observations and fixed-run ECMWF guidance,
  schedules shared forecasts and refuses expired results. Customer reads never run inference.
- API keys, per-key rate limits, account-scoped idempotency and atomic credit reservations
  protect replay submissions. PostgreSQL holds the queue, worker leases and forecasts.
- A separate GPU worker validates provenance, runs the pinned model and publishes results.
  Retries, fenced leases and terminal-failure refunds handle interrupted jobs.
- Hourly scoring compares published live forecasts against later observations. It runs
  as a CronJob on ordinary compute, never on the GPU, so it survives a scaled-down worker.
- An opt-in research loop fine-tunes the output head on the same GPU between forecast
  windows, at most weekly with a ten-minute budget. Training and inference never run
  together; candidates require review and never automatically replace the serving
  checkpoint, and a candidate is rejected outright when its Ridge comparator is
  extrapolating rather than comparing.
  See [operation](deploy/README.md#single-gpu-research-learning).
- Optional KEDA autoscaling takes the GPU worker to zero replicas on an empty queue.
  It is mutually exclusive with the research loop, and reaching zero *cost* also needs
  a node autoscaler. See [scoring, autoscaling and alerts](deploy/README.md#scoring-autoscaling-and-alerts).
- Ordinary compute runs the API and ingestion. **Model inference requires CUDA on AWS**
  (or native MPS for local Mac development); there is no CPU inference fallback.

## Accuracy Evidence

The [multi-season protocol](experiments/weather_seasonal_v1.json) compares all methods against
the same unfilled NOAA station observations, with separate training, development and
test periods.

| Method | Test RMSE |
| --- | --- |
| Unmodified ECMWF guidance | 1.718 C |
| TimesFM + ECMWF guidance | 1.366 C |
| Ridge correction | 1.359 C |

TimesFM + ECMWF had **20.52% lower RMSE than unmodified ECMWF**, across 4,271 matched
hours from 90 accepted cases out of 99 scheduled. The paired 95% difference interval
was [-0.485, -0.213] C. Ridge was slightly better than TimesFM.

This is a limited retrospective three-station result, not proof of beating Indus-wx,
all weather models or ECMWF across India. The live feed has different source/QC rules
and needs prospective validation. Quantile calibration is not established.

The [holdout manifest](results/weather/india_station_seasonal_v1/holdout/manifest.json)
records scores, coverage and provenance. Earlier experiments remain in
[the case runner](scripts/weather_case.py),
[the backtest protocol](experiments/weather_backtest_v1.json) and
[the correction protocol](experiments/weather_correction_v1.json).

## System Design

```text
NOAA observations + ECMWF guidance
                 |
          Scheduled ingestor
                 |
                 v
        PostgreSQL database <----> TimesFM GPU worker
        inputs / queue / results      claim job, infer, publish
                 ^
                 | read stored results / enqueue replays
                 v
           FastAPI backend
                 ^
                 |
        HTTPS gateway + load balancer
                 ^
                 |
          Customer / API client
```

AWS uses EKS, private RDS, S3 artifacts and Secrets Manager. The September 5 deployment
record verifies three real HTTPS/CUDA replays, idempotency and reference agreement.
It does not establish live accuracy, high availability or production readiness.
The account release passed Trivy and ECR gates for its API/bootstrap images. The
research-learning release also rebuilds and scans the worker and bootstrap images;
ingestion remains unchanged. Review current findings before external use. Running
AWS resources continue to incur charges; no automatic shutdown is configured.

## Code Map

| Location | Responsibility |
| --- | --- |
| `timesfm_serve/weather_api.py` | HTTP endpoints, authentication and request bounds |
| `frontend/` | Weather dashboard, archived benchmarks and local live API adapter |
| `timesfm_serve/weather_store.py` | Durable jobs, credits, leases and results |
| `timesfm_serve/weather_worker.py` | GPU worker lifecycle and recovery |
| `timesfm_serve/weather_gpu.py` | Single-GPU inference/training handoff and recovery |
| `timesfm_serve/weather_learning.py`, `weather_training.py` | Delayed labels, chronological datasets, fine-tuning and evaluation gates |
| `timesfm_serve/weather_metrics.py`, `scripts/weather_score.py` | Exported metrics and off-GPU hourly scoring |
| `timesfm_serve/weather_engine.py` | Frozen-input validation and TimesFM inference |
| `timesfm_serve/weather_ingest.py`, `weather_live_*.py` | Live input policy, capture and publication |
| `timesfm_serve/auth.py`, `db.py`, `database_config.py` | Shared identities, database connections and migrations |
| `scripts/weather_*.py` | Experiments, ingestion, smoke checks and deployment utilities |
| `migrations/` | Ordered SQL schema history; retain all migrations |
| `experiments/weather_*.json`, `results/weather/` | Experiment definitions and reproducibility evidence |
| `infra/bootstrap/`, `infra/runtime/` | AWS setup foundation and application infrastructure |
| `deploy/` | Kubernetes configuration and deployment templates |
| `deploy/Dockerfile.*` | Separate API, ingestion, bootstrap and CUDA worker images |
| `tests/` | Regression coverage |

## Local Development

Requires Docker Compose, Python 3.12+ and uv. From this directory:

```bash
uv sync --frozen
docker compose up -d --build --wait
export DATABASE_URL=postgresql://tfm:tfm@localhost:15432/tfm
export WEATHER_API_KEY="$(uv run --no-sync python -m scripts.create_key weather-demo replay-demo)"
curl --fail http://127.0.0.1:18001/health/ready
curl --fail -H "x-api-key: $WEATHER_API_KEY" http://127.0.0.1:18001/v1/weather/stations
```

Open `http://127.0.0.1:18001/docs`. Compose starts only PostgreSQL and the lightweight
API, with an isolated `pravah-weather-local` project/volume and loopback ports 15432
and 18001. Set `WEATHER_DB_PORT` / `WEATHER_API_PORT` to change them and adjust the
host commands accordingly. Old demo containers and volumes are not reused or stopped.

**A GPU worker must also be running for queued forecasts to complete.**
Provision the pinned model snapshot specified in [weather_model.py](scripts/weather_model.py)
before worker startup. On a Mac, start one native MPS worker in a separate terminal:

```bash
DATABASE_URL=postgresql://tfm:tfm@localhost:15432/tfm \
  .venv/bin/python -m timesfm_serve.weather_worker \
  --device mps --cache-dir /path/to/huggingface/hub
```

Do not start a second worker on the same GPU. AWS uses `deploy/Dockerfile.worker`
with CUDA and offline artifacts. To verify a complete replay through the local API
and GPU worker, run from the terminal containing `WEATHER_API_KEY`:

```bash
.venv/bin/python -m scripts.weather_api_smoke \
  --base-url http://127.0.0.1:18001 --verify-reference
```

Local credentials in Compose are for development only. Do not expose these ports
publicly. Keep real keys, model caches, Terraform state and rendered secret-bearing
configuration out of Git.

## API

| Method | Route |
| --- | --- |
| GET | `/v1/weather/stations` |
| GET | `/v1/weather/replays` |
| POST | `/v1/weather/replays` |
| GET | `/v1/weather/jobs/{job_id}` |
| GET | `/v1/weather/forecasts/{job_id}` |
| GET | `/v1/weather/stations/{station_id}/latest` |
| GET | `/health/live`, `/health/ready` |

Weather routes require `x-api-key`; replay submission also requires `Idempotency-Key`.
GitHub users can issue read-only keys; replay credits and replay-capable keys remain
operator-managed. Replay costs 48 credits, reserved once per
unique submission. This is metered demo access, not a payment integration.
Identical replay submissions with the same idempotency key reuse the job; conflicting
reuse returns 409. Job and forecast reads are owner-only. Live reads never fall back
to historical replays; expiry rules are defined in
[weather_live_policy.py](timesfm_serve/weather_live_policy.py).

## Tests

Create a disposable database once, then run:

```bash
docker compose exec db createdb -U tfm tfm_test
DATABASE_URL=postgresql://tfm:tfm@localhost:15432/tfm_test uv run --no-sync pytest -q
uv run --no-sync ruff check .
```

Tests reject non-`*_test` databases and mounted runtime credentials before importing
the application. SQL/concurrency tests use real PostgreSQL; inference is mocked in
queue tests. Actual GPU replay verification is a separate smoke test, not claimed by
a passing unit suite. CI also builds the non-ML containers and validates Terraform
and Kubernetes configuration.

## History And Scope

The former electricity-demand demo, Redis queue, OAuth dashboard and old deployment
scripts are removed from the active tree, **not from Git history**. The remote remains
`git@github.com:Gmin2/timesfm-serve.git`; the `typescript-gateway` branch is retained.
The old demand demo remains at commit `14002b05619c7894e3b65bb19c1970a3d06bd32a`.
All applied SQL migrations are retained. Local operating notes in `docs/` are
Git-ignored and are not required to run the project or its tests.

This project demonstrates forecasting infrastructure and API engineering.
Public live dashboard reads, prospective evaluation and operational
hardening remain further work. Review the pinned TimesFM model's license before any commercial use.
