import logging
import time
from contextlib import asynccontextmanager

import numpy as np
import timesfm
from fastapi import Depends, FastAPI, Request
from pydantic import BaseModel, Field

from timesfm_serve import db
from timesfm_serve.auth import require_key

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("timesfm_serve")

MODEL_ID = "google/timesfm-3.0-pytorch"
state = {}


@asynccontextmanager
async def lifespan(app):
    db.init()
    state["model"] = timesfm.TimesFM3Forecaster.from_pretrained(MODEL_ID, device="cpu")
    yield
    state.clear()


app = FastAPI(title="timesfm-serve", lifespan=lifespan)


@app.middleware("http")
async def timing(request: Request, call_next):
    t0 = time.perf_counter()
    resp = await call_next(request)
    ms = (time.perf_counter() - t0) * 1000
    resp.headers["x-response-time-ms"] = f"{ms:.1f}"
    log.info("%s %s %s %.1fms", request.method, request.url.path, resp.status_code, ms)
    return resp


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
    db.record_run(tenant, MODEL_ID, req.horizon, len(ctx), 0 if past is None else len(past), 0 if fut is None else len(fut), ms)
    return ForecastResponse(
        forecast=out.forecast.tolist(),
        quantiles=out.quantiles.tolist(),
        quantile_levels=[round(q, 1) for q in np.arange(0.1, 1.0, 0.1)],
        model=MODEL_ID,
    )
