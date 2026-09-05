import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from datetime import date, timedelta

import numpy as np
import timesfm
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from timesfm_serve import dashboard, db, demo, jobs, metrics, oauth, weather
from timesfm_serve.auth import Account, require_key

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("timesfm_serve")

MODEL_ID = os.environ.get("MODEL_ID", "google/timesfm-3.0-pytorch")
# cpu in containers. "mps" runs on the apple gpu when python runs natively on
# a mac, which is roughly 2x and is what the benchmark script uses.
DEVICE = os.environ.get("DEVICE", "cpu")
QUANTILE_LEVELS = [round(q, 1) for q in np.arange(0.1, 1.0, 0.1)]
state = {}


def jlog(level: str, msg: str, **fields):
    log.info(json.dumps({"level": level, "msg": msg, **fields}))


@asynccontextmanager
async def lifespan(app):
    db.migrate()
    state["model"] = timesfm.TimesFM3Forecaster.from_pretrained(MODEL_ID, device=DEVICE)
    metrics.MODEL_INFO.labels(model=MODEL_ID, device=DEVICE).set(1)
    jlog("info", "model loaded", model=MODEL_ID, device=DEVICE)
    yield
    state.clear()


app = FastAPI(title="timesfm-serve", lifespan=lifespan)
app.include_router(demo.router)
app.include_router(oauth.router)
app.include_router(dashboard.router)


@app.middleware("http")
async def observability(request: Request, call_next):
    request.state.request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
    t0 = time.perf_counter()
    status = 500
    resp = None
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
        jlog(
            "info",
            "request",
            request_id=request.state.request_id,
            method=request.method,
            path=request.url.path,
            status=status,
            ms=round(ms, 1),
            **({"account": request.state.account} if hasattr(request.state, "account") else {}),
        )
        if resp is not None:
            resp.headers["x-request-id"] = request.state.request_id
            resp.headers["x-response-time-ms"] = f"{ms:.1f}"
            limit = getattr(request.state, "rate_limit", None)
            if limit:
                resp.headers["x-ratelimit-limit"] = str(limit[0])
                resp.headers["x-ratelimit-remaining"] = str(limit[1])
                resp.headers["x-ratelimit-reset"] = str(limit[2])
            remaining = getattr(request.state, "credits_remaining", None)
            if remaining is not None:
                resp.headers["x-credits-remaining"] = str(remaining)


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
    credits_charged: int
    credits_remaining: int


def spend(request: Request, account: Account, points: int) -> int:
    """reserve credits before the model runs, or 402."""
    remaining = db.reserve_credits(account.id, points)
    if remaining is None:
        current = db.usage(account.id) or {}
        raise HTTPException(
            402,
            {
                "error": "out of credits",
                "credits_remaining": current.get("credits_remaining", 0),
                "credits_needed": points,
            },
        )
    request.state.credits_remaining = remaining
    return remaining


@app.get("/metrics")
def prom_metrics():
    return metrics.render(lambda: len(jobs.queue()))


@app.get("/health")
def health():
    return {"ok": True, "model_loaded": "model" in state}


@app.get("/usage")
def get_usage(account: Account = Depends(require_key)):
    found = db.usage(account.id)
    if found is None:
        raise HTTPException(404, "account not found")
    return found


@app.get("/weather/providers")
def weather_providers():
    return {
        "providers": weather.provider_names(),
        "default": os.environ.get("WEATHER_PROVIDER", "open-meteo"),
    }


@app.post("/forecast", response_model=ForecastResponse)
def forecast(req: ForecastRequest, request: Request, account: Account = Depends(require_key)):
    request.state.account = account.name
    ctx = np.asarray(req.series, dtype=np.float32)
    past = np.asarray(req.past_covariates, dtype=np.float32) if req.past_covariates else None
    fut = np.asarray(req.future_covariates, dtype=np.float32) if req.future_covariates else None

    _check_covariates(len(ctx), req.horizon, past, fut)

    # one series through this endpoint, so the cost is just the horizon
    points = req.horizon
    remaining = spend(request, account, points)

    try:
        t0 = time.perf_counter()
        out = state["model"].predict(
            ctx,
            horizon=req.horizon,
            past_only_covariates=past,
            past_future_covariates=fut,
            return_quantiles=True,
        )
        ms = (time.perf_counter() - t0) * 1000
    except Exception:
        db.refund_credits(account.id, points)
        raise

    metrics.FORECAST_SECONDS.labels("sync").observe(ms / 1000)
    metrics.FORECAST_SERIES.labels("sync").inc()
    db.record_run(
        account.id,
        MODEL_ID,
        req.horizon,
        len(ctx),
        0 if past is None else len(past),
        0 if fut is None else len(fut),
        points,
        ms,
        request.state.request_id,
    )
    return ForecastResponse(
        forecast=out.forecast.tolist(),
        quantiles=out.quantiles.tolist(),
        quantile_levels=QUANTILE_LEVELS,
        model=MODEL_ID,
        credits_charged=points,
        credits_remaining=remaining,
    )


def _check_covariates(context_len: int, horizon: int, past, future) -> None:
    """the model silently pads or truncates covariates of the wrong length,
    which hides a caller mistake and quietly ruins the forecast. reject it."""
    if past is not None and past.shape[1] != context_len:
        raise HTTPException(
            422, f"each past covariate needs {context_len} points, got {past.shape[1]}"
        )
    need = context_len + horizon
    if future is not None and future.shape[1] != need:
        raise HTTPException(
            422,
            f"each future covariate needs {need} points (series + horizon), got {future.shape[1]}",
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
def submit_job(req: JobRequest, request: Request, account: Account = Depends(require_key)):
    request.state.account = account.name
    job_id = str(uuid.uuid4())

    # a batch costs one credit per forecast step per series
    points = len(req.items) * req.horizon
    spend(request, account, points)

    db.create_job(job_id, account.id, len(req.items), req.horizon, points)
    jobs.queue().enqueue(
        jobs.run_batch,
        job_id,
        [it.model_dump() for it in req.items],
        req.horizon,
        job_id=job_id,
        job_timeout=3600,
    )
    return {"job_id": job_id, "status": "queued", "credits_charged": points}


@app.get("/jobs/{job_id}")
def get_job(job_id: uuid.UUID, request: Request, account: Account = Depends(require_key)):
    request.state.account = account.name
    job_id = str(job_id)
    found = db.get_job(job_id, account.id)
    if found is None:
        raise HTTPException(404, "job not found")
    (status, *_), _ = found
    if jobs.reconcile(job_id, status) != status:
        found = db.get_job(job_id, account.id)
    (status, n_series, horizon, points, error, created, started, finished), results = found
    return {
        "job_id": job_id,
        "status": status,
        "n_series": n_series,
        "horizon": horizon,
        "credits_charged": points,
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
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    horizon: int = Field(default=14, ge=1, le=90)
    provider: str = Field(default_factory=lambda: os.environ.get("WEATHER_PROVIDER", "open-meteo"))


@app.post("/forecast/weather", response_model=ForecastResponse)
def forecast_with_weather(
    req: WeatherForecastRequest, request: Request, account: Account = Depends(require_key)
):
    """daily demand plus a lat lon. weather covariates come from the provider."""
    request.state.account = account.name
    try:
        prov = weather.get_provider(req.provider)
    except KeyError:
        raise HTTPException(400, f"unknown weather provider {req.provider}") from None

    start = req.last_date - timedelta(days=len(req.series) - 1)
    end = req.last_date + timedelta(days=req.horizon)

    # weather is fetched before any credit is reserved, so a provider that is
    # down or not implemented never costs the caller anything
    try:
        cov = prov.covariates(req.lat, req.lon, start, end, freq="D")
    except NotImplementedError as e:
        raise HTTPException(501, str(e)) from None

    expected = len(req.series) + req.horizon
    if cov.shape[1] != expected:
        raise HTTPException(502, f"provider returned {cov.shape[1]} steps, expected {expected}")

    points = req.horizon
    remaining = spend(request, account, points)

    try:
        t0 = time.perf_counter()
        out = state["model"].predict(
            np.asarray(req.series, dtype=np.float32),
            horizon=req.horizon,
            past_future_covariates=cov,
            return_quantiles=True,
        )
        ms = (time.perf_counter() - t0) * 1000
    except Exception:
        db.refund_credits(account.id, points)
        raise

    metrics.FORECAST_SECONDS.labels("weather").observe(ms / 1000)
    metrics.FORECAST_SERIES.labels("weather").inc()
    db.record_run(
        account.id,
        MODEL_ID,
        req.horizon,
        len(req.series),
        0,
        len(cov),
        points,
        ms,
        request.state.request_id,
    )
    return ForecastResponse(
        forecast=out.forecast.tolist(),
        quantiles=out.quantiles.tolist(),
        quantile_levels=QUANTILE_LEVELS,
        model=MODEL_ID,
        credits_charged=points,
        credits_remaining=remaining,
    )


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    detail = exc.detail if isinstance(exc.detail, dict) else {"error": exc.detail}
    return JSONResponse(status_code=exc.status_code, content=detail, headers=exc.headers)
