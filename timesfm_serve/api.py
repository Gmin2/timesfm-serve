from contextlib import asynccontextmanager

import numpy as np
import timesfm
from fastapi import FastAPI
from pydantic import BaseModel, Field

MODEL_ID = "google/timesfm-3.0-pytorch"
state = {}


@asynccontextmanager
async def lifespan(app):
    state["model"] = timesfm.TimesFM3Forecaster.from_pretrained(MODEL_ID, device="cpu")
    yield
    state.clear()


app = FastAPI(title="timesfm-serve", lifespan=lifespan)


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
def forecast(req: ForecastRequest):
    ctx = np.asarray(req.series, dtype=np.float32)
    past = np.asarray(req.past_covariates, dtype=np.float32) if req.past_covariates else None
    fut = np.asarray(req.future_covariates, dtype=np.float32) if req.future_covariates else None
    out = state["model"].predict(
        ctx,
        horizon=req.horizon,
        past_only_covariates=past,
        past_future_covariates=fut,
        return_quantiles=True,
    )
    return ForecastResponse(
        forecast=out.forecast.tolist(),
        quantiles=out.quantiles.tolist(),
        quantile_levels=[round(q, 1) for q in np.arange(0.1, 1.0, 0.1)],
        model=MODEL_ID,
    )
