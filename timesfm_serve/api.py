import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from datetime import date, timedelta

import numpy as np
import timesfm
from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from timesfm_serve import db, jobs, metrics, weather
from timesfm_serve.auth import require_key

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("timesfm_serve")

MODEL_ID = "google/timesfm-3.0-pytorch"
state = {}


@asynccontextmanager
async def lifespan(app):
    db.init()
    state["model"] = timesfm.TimesFM3Forecaster.from_pretrained(MODEL_ID, device="cpu")
    metrics.MODEL_INFO.labels(model=MODEL_ID, device="cpu").set(1)
    yield
    state.clear()


app = FastAPI(title="timesfm-serve", lifespan=lifespan)


@app.middleware("http")
async def timing(request: Request, call_next):
    t0 = time.perf_counter()
    status = 500
    try:
        resp = await call_next(request)
        status = resp.status_code
        return resp
    finally:
        ms = (time.perf_counter() - t0) * 1000
        route = request.scope.get("route")
        path = route.path if route else request.url.path
        metrics.REQUESTS.labels(request.method, path, status).inc()
        metrics.LATENCY.labels(request.method, path).observe(ms / 1000)
        log.info("%s %s %s %.1fms", request.method, request.url.path, status, ms)
        if status != 500:
            resp.headers["x-response-time-ms"] = f"{ms:.1f}"


class ForecastRequest(BaseModel):
    series: list[float] = Field(min_length=8)
    horizon: int = Field(default=14, ge=1, le=1000)
    past_covariates: list[list[float]] | None = None
    future_covariates: list[list[float]] | None = None


class ForecastResponse(BaseModel):
    forecast: list[float]
    quantiles: list[list[float]]
    quantile_levels: list[float]
    model: str


@app.get("/metrics")
def prom_metrics():
    return metrics.render(lambda: len(jobs.queue()))


@app.get("/health")
def health():
    return {"ok": True, "model_loaded": "model" in state}


@app.post("/forecast", response_model=ForecastResponse)
def forecast(req: ForecastRequest, tenant: str = Depends(require_key)):
    ctx = np.asarray(req.series, dtype=np.float32)
    past = np.asarray(req.past_covariates, dtype=np.float32) if req.past_covariates else None
    fut = np.asarray(req.future_covariates, dtype=np.float32) if req.future_covariates else None
    t0 = time.perf_counter()
    out = state["model"].predict(
        ctx,
        horizon=req.horizon,
        past_only_covariates=past,
        past_future_covariates=fut,
        return_quantiles=True,
    )
    ms = (time.perf_counter() - t0) * 1000
    metrics.FORECAST_SECONDS.labels("sync").observe(ms / 1000)
    metrics.FORECAST_SERIES.labels("sync").inc()
    db.record_run(tenant, MODEL_ID, req.horizon, len(ctx), 0 if past is None else len(past), 0 if fut is None else len(fut), ms)
    return ForecastResponse(
        forecast=out.forecast.tolist(),
        quantiles=out.quantiles.tolist(),
        quantile_levels=[round(q, 1) for q in np.arange(0.1, 1.0, 0.1)],
        model=MODEL_ID,
    )


class SeriesItem(BaseModel):
    id: str
    series: list[float] = Field(min_length=8)
    past_covariates: list[list[float]] | None = None
    future_covariates: list[list[float]] | None = None


class JobRequest(BaseModel):
    items: list[SeriesItem] = Field(min_length=1, max_length=5000)
    horizon: int = Field(default=14, ge=1, le=1000)


@app.post("/jobs", status_code=202)
def submit_job(req: JobRequest, tenant: str = Depends(require_key)):
    job_id = str(uuid.uuid4())
    db.create_job(job_id, tenant, len(req.items), req.horizon)
    jobs.queue().enqueue(jobs.run_batch, job_id, [it.model_dump() for it in req.items], req.horizon, job_id=job_id, job_timeout=3600)
    return {"job_id": job_id, "status": "queued"}


@app.get("/jobs/{job_id}")
def get_job(job_id: uuid.UUID, tenant: str = Depends(require_key)):
    job_id = str(job_id)
    found = db.get_job(job_id, tenant)
    if found is None:
        raise HTTPException(404, "job not found")
    (status, n_series, horizon, error, created, started, finished), results = found
    if jobs.reconcile(job_id, status) != status:
        (status, n_series, horizon, error, created, started, finished), results = db.get_job(job_id, tenant)
    return {
        "job_id": job_id,
        "status": status,
        "n_series": n_series,
        "horizon": horizon,
        "error": error,
        "created_at": created,
        "started_at": started,
        "finished_at": finished,
        "results": [{"id": sid, "forecast": f, "quantiles": q} for sid, f, q in results],
    }


class WeatherForecastRequest(BaseModel):
    """demand history for one location. the service fetches weather itself."""

    series: list[float] = Field(min_length=8)
    last_date: date
    lat: float
    lon: float
    horizon: int = Field(default=14, ge=1, le=90)
    provider: str = Field(default_factory=lambda: os.environ.get("WEATHER_PROVIDER", "open-meteo"))


@app.post("/forecast/weather", response_model=ForecastResponse)
def forecast_with_weather(req: WeatherForecastRequest, tenant: str = Depends(require_key)):
    """daily demand plus a lat lon. weather covariates come from the configured provider."""
    try:
        prov = weather.get_provider(req.provider)
    except KeyError:
        raise HTTPException(400, f"unknown weather provider {req.provider}") from None
    start = req.last_date - timedelta(days=len(req.series) - 1)
    end = req.last_date + timedelta(days=req.horizon)
    try:
        cov = prov.covariates(req.lat, req.lon, start, end, freq="D")
    except NotImplementedError as e:
        raise HTTPException(501, str(e)) from None
    expected = len(req.series) + req.horizon
    if cov.shape[1] != expected:
        raise HTTPException(502, f"provider returned {cov.shape[1]} steps, expected {expected}")
    t0 = time.perf_counter()
    out = state["model"].predict(np.asarray(req.series, dtype=np.float32), horizon=req.horizon, past_future_covariates=cov, return_quantiles=True)
    ms = (time.perf_counter() - t0) * 1000
    metrics.FORECAST_SECONDS.labels("weather").observe(ms / 1000)
    metrics.FORECAST_SERIES.labels("weather").inc()
    db.record_run(tenant, MODEL_ID, req.horizon, len(req.series), 0, len(cov), ms)
    return ForecastResponse(forecast=out.forecast.tolist(), quantiles=out.quantiles.tolist(), quantile_levels=[round(q, 1) for q in np.arange(0.1, 1.0, 0.1)], model=MODEL_ID)
