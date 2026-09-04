import logging
import os
import time
from contextlib import asynccontextmanager

import numpy as np
import timesfm
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

MODEL_ID = os.environ.get("MODEL_ID", "google/timesfm-3.0-pytorch")
DEVICE = os.environ.get("DEVICE", "cpu")

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("inference")
state = {}


@asynccontextmanager
async def lifespan(app):
    state["model"] = timesfm.TimesFM3Forecaster.from_pretrained(MODEL_ID, device=DEVICE)
    log.info('{"msg": "model loaded", "model": "%s", "device": "%s"}', MODEL_ID, DEVICE)
    yield
    state.clear()


app = FastAPI(title="timesfm-inference", lifespan=lifespan)


class PredictRequest(BaseModel):
    series: list[list[float]] = Field(min_length=1)
    horizon: int = Field(ge=1, le=1000)
    past_covariates: list[list[list[float]] | None] | None = None
    future_covariates: list[list[list[float]] | None] | None = None


class Prediction(BaseModel):
    forecast: list[float]
    quantiles: list[list[float]]


class PredictResponse(BaseModel):
    predictions: list[Prediction]
    model: str
    device: str
    latency_ms: float


def as_arrays(covariates, n):
    if covariates is None:
        return None
    if len(covariates) != n:
        raise HTTPException(422, f"got {len(covariates)} covariate sets for {n} series")
    return [None if c is None else np.asarray(c, dtype=np.float32) for c in covariates]


@app.get("/health")
def health():
    return {"ok": True, "model_loaded": "model" in state, "model": MODEL_ID, "device": DEVICE}


@app.post("/predict", response_model=PredictResponse)
def predict(req: PredictRequest):
    contexts = [np.asarray(s, dtype=np.float32) for s in req.series]
    for i, c in enumerate(contexts):
        if not np.isfinite(c).all():
            raise HTTPException(422, f"series {i} has nan or inf")

    n = len(contexts)
    past = as_arrays(req.past_covariates, n)
    future = as_arrays(req.future_covariates, n)

    t0 = time.perf_counter()
    outs = list(
        state["model"].predict_batch(
            contexts,
            horizon=req.horizon,
            past_only_covariates=past,
            past_future_covariates=future,
            return_quantiles=True,
        )
    )
    ms = (time.perf_counter() - t0) * 1000
    log.info('{"msg": "predict", "n": %d, "horizon": %d, "ms": %.1f}', n, req.horizon, ms)

    return PredictResponse(
        predictions=[
            Prediction(forecast=o.forecast.tolist(), quantiles=o.quantiles.tolist()) for o in outs
        ],
        model=MODEL_ID,
        device=DEVICE,
        latency_ms=ms,
    )
