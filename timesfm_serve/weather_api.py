"""Weather delivery API. Model loading belongs exclusively to the worker."""

import json
import logging
import time
import uuid
from contextlib import asynccontextmanager
from typing import Annotated

import psycopg
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from psycopg_pool import PoolTimeout
from pydantic import BaseModel, ConfigDict, Field

from timesfm_serve import customer_auth, db, weather_catalog, weather_live_store, weather_metrics, weather_store
from timesfm_serve.auth import Account, require_key
from timesfm_serve.weather_contract import ForecastDocument, ReplaySubmission, WeatherJob
from timesfm_serve.weather_live_contract import LiveForecastResponse

logger = logging.getLogger("uvicorn.error")

STATIONS = [
    {"id": "42410099999", "name": "Guwahati", "latitude": 26.1, "longitude": 91.58},
    {"id": "43128599999", "name": "Hyderabad", "latitude": 17.2333, "longitude": 78.4167},
    {"id": "43279099999", "name": "Chennai", "latitude": 12.9944, "longitude": 80.1805},
]


@asynccontextmanager
async def lifespan(app):
    customer_auth.settings()
    db.initialize()
    yield
    db.pool.close()


class BodyLimit:
    def __init__(self, app, max_bytes=8192):
        self.app, self.max_bytes = app, max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "POST":
            return await self.app(scope, receive, send)
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            if len(body) + len(chunk) > self.max_bytes:
                return await JSONResponse({"detail": "request_too_large"}, status_code=413)(scope, receive, send)
            body.extend(chunk)
            if not message.get("more_body", False):
                break

        async def replay_body():
            nonlocal body
            if body is not None:
                value, body = bytes(body), None
                return {"type": "http.request", "body": value, "more_body": False}
            return await receive()

        return await self.app(scope, replay_body, send)


app = FastAPI(title="Weather Forecast API", version="0.2.0", lifespan=lifespan)
app.add_middleware(BodyLimit)
app.include_router(customer_auth.router)
app.add_api_route("/metrics", weather_metrics.metrics, methods=["GET"], include_in_schema=False)


@app.middleware("http")
async def request_logging(request: Request, call_next):
    request_id = str(uuid.uuid4())
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception as exc:
        logger.error(json.dumps({"event": "weather_request_error", "request_id": request_id, "exception_type": type(exc).__name__}))
        response = JSONResponse({"detail": "internal_error"}, status_code=500)
    response.headers["x-request-id"] = request_id
    response.headers["cache-control"] = "no-store"
    if limits := getattr(request.state, "rate_limit", None):
        for name, value in zip(("limit", "remaining", "reset"), limits, strict=True):
            response.headers[f"x-ratelimit-{name}"] = str(value)
    route = getattr(request.scope.get("route"), "path", "unmatched")
    method = request.method if request.method in ("GET", "POST", "HEAD", "OPTIONS") else "OTHER"
    elapsed = time.perf_counter() - started
    weather_metrics.REQUESTS.labels(method, route, response.status_code).inc()
    weather_metrics.LATENCY.labels(method, route).observe(elapsed)
    logger.info(json.dumps({
        "event": "weather_request", "request_id": request_id,
        "method": request.method,
        "route": route,
        "status": response.status_code,
        "duration_ms": round(elapsed * 1000, 2),
    }))
    return response


@app.exception_handler(psycopg.Error)
@app.exception_handler(PoolTimeout)
async def database_unavailable(request, exc):
    return JSONResponse({"detail": "database_unavailable"}, status_code=503, headers={"retry-after": "5"})


@app.get("/health/live")
def live():
    return {"status": "ok", "service": "weather-api"}


@app.get("/health/ready")
def ready():
    with db.conn() as c:
        c.execute("select 1 from accounts, weather_jobs, weather_forecasts, weather_live_inputs, weather_ingestion_status limit 0")
    return {"status": "ready"}


@app.get("/v1/weather/stations", dependencies=[Depends(require_key)])
def stations():
    status = [weather_live_store.station_status(station["id"]) for station in STATIONS]
    return {
        "stations": STATIONS, "modes": ["historical_replay", "experimental_live"],
        "live_forecasts_available": any(item["available"] for item in status), "live_status": status,
    }


def require_station(station_id):
    if station_id not in {station["id"] for station in STATIONS}:
        raise HTTPException(404, "station_not_found")


@app.get("/v1/weather/stations/{station_id}/latest", response_model=LiveForecastResponse, dependencies=[Depends(require_key)])
def latest_forecast(station_id: str):
    require_station(station_id)
    document, reason = weather_live_store.latest(station_id)
    if document is None:
        raise HTTPException(503, reason, headers={"retry-after": "300"})
    return document


@app.get("/v1/weather/stations/{station_id}/status", dependencies=[Depends(require_key)])
def live_status(station_id: str):
    require_station(station_id)
    return weather_live_store.station_status(station_id)


class ReplayRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    case_id: str = Field(pattern=f"^{weather_catalog.CASE_PATTERN}$", max_length=32)


@app.get("/v1/weather/replays", dependencies=[Depends(require_key)])
def replays():
    return {"mode": "historical_replay", "cases": weather_catalog.catalog()}


@app.post("/v1/weather/replays", status_code=202, response_model=ReplaySubmission, responses={200: {"model": ReplaySubmission}})
def submit_replay(
    body: ReplayRequest,
    response: Response,
    idempotency_key: Annotated[str, Header(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")],
    account: Account = Depends(require_key),
):
    if account.read_only:
        raise HTTPException(403, "read_only_api_key")
    try:
        _, _, digest = weather_catalog.case_manifest(body.case_id)
    except (OSError, ValueError):
        raise HTTPException(404, "replay_case_unavailable") from None
    try:
        job_id, created = weather_store.submit(account.id, idempotency_key, body.case_id, digest)
    except weather_store.SubmissionError as exc:
        headers = {"retry-after": "30"} if exc.status in (429, 503) else None
        raise HTTPException(exc.status, exc.code, headers=headers) from None
    response.status_code = 202 if created else 200
    response.headers["location"] = f"/v1/weather/jobs/{job_id}"
    return {"job_id": job_id, "created": created, "mode": "historical_replay"}


@app.get("/v1/weather/jobs/{job_id}", response_model=WeatherJob)
def job(job_id: uuid.UUID, account: Account = Depends(require_key)):
    result = weather_store.get_job(job_id, account.id)
    if result is None:
        raise HTTPException(404, "job_not_found")
    result["mode"] = "historical_replay"
    result["forecast_url"] = f"/v1/weather/forecasts/{job_id}" if result["status"] == "succeeded" else None
    return result


@app.get("/v1/weather/forecasts/{job_id}", response_model=ForecastDocument)
def forecast(job_id: uuid.UUID, account: Account = Depends(require_key)):
    result = weather_store.get_forecast(job_id, account.id)
    if result is None:
        raise HTTPException(404, "forecast_not_found")
    return result
