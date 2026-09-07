import os
import secrets
from pathlib import Path

from fastapi import Header, HTTPException
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Gauge, Histogram, generate_latest
from starlette.responses import Response

from timesfm_serve import db

registry = CollectorRegistry()
REQUESTS = Counter("weather_http_requests_total", "Weather API requests", ["method", "route", "status"], registry=registry)
LATENCY = Histogram("weather_http_request_seconds", "Weather API request latency", ["method", "route"], registry=registry)
JOBS = Gauge("weather_jobs", "Durable weather jobs", ["status"], registry=registry)
OLDEST = Gauge("weather_oldest_pending_seconds", "Age of the oldest queued or running job", registry=registry)
INGEST_AGE = Gauge("weather_ingestion_age_seconds", "Seconds since the last live ingestion attempt", ["station_id"], registry=registry)
INGEST_OK = Gauge("weather_ingestion_ok", "1 when the last live ingestion attempt was accepted", ["station_id"], registry=registry)


def token():
    """The scrape credential is a mounted file in the cluster, an env var locally."""
    if path := os.environ.get("WEATHER_METRICS_TOKEN_FILE"):
        return Path(path).read_text().strip()
    return os.environ.get("WEATHER_METRICS_TOKEN")


def metrics(authorization: str | None = Header(default=None)):
    expected = token()
    if not expected:
        raise HTTPException(404, "not_found")
    if not secrets.compare_digest((authorization or "").encode(), f"Bearer {expected}".encode()):
        raise HTTPException(401, "invalid_metrics_token")
    with db.conn() as c:
        counts = dict(c.execute("select status, count(*) from weather_jobs group by status").fetchall())
        age = c.execute(
            "select coalesce(extract(epoch from (clock_timestamp() - min(created_at))), 0)"
            " from weather_jobs where status in ('queued', 'running')"
        ).fetchone()[0]
        ingestion = c.execute(
            "select station_id, extract(epoch from (clock_timestamp() - attempted_at)), status"
            " from weather_ingestion_status"
        ).fetchall()
    for status in ("queued", "running", "succeeded", "failed"):
        JOBS.labels(status).set(counts.get(status, 0))
    OLDEST.set(float(age))
    for station_id, since, status in ingestion:
        INGEST_AGE.labels(station_id).set(float(since))
        INGEST_OK.labels(station_id).set(float(status in ("queued", "existing")))
    return Response(generate_latest(registry), media_type=CONTENT_TYPE_LATEST)
