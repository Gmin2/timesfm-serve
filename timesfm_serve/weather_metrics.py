import os
import secrets

from fastapi import Header, HTTPException
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Gauge, Histogram, generate_latest
from starlette.responses import Response

from timesfm_serve import db

registry = CollectorRegistry()
REQUESTS = Counter("weather_http_requests_total", "Weather API requests", ["method", "route", "status"], registry=registry)
LATENCY = Histogram("weather_http_request_seconds", "Weather API request latency", ["method", "route"], registry=registry)
JOBS = Gauge("weather_jobs", "Durable weather jobs", ["status"], registry=registry)
OLDEST = Gauge("weather_oldest_pending_seconds", "Age of the oldest queued or running job", registry=registry)


def metrics(authorization: str | None = Header(default=None)):
    token = os.environ.get("WEATHER_METRICS_TOKEN")
    if not token:
        raise HTTPException(404, "not_found")
    if not secrets.compare_digest((authorization or "").encode(), f"Bearer {token}".encode()):
        raise HTTPException(401, "invalid_metrics_token")
    with db.conn() as c:
        counts = dict(c.execute("select status, count(*) from weather_jobs group by status").fetchall())
        age = c.execute(
            "select coalesce(extract(epoch from (clock_timestamp() - min(created_at))), 0)"
            " from weather_jobs where status in ('queued', 'running')"
        ).fetchone()[0]
    for status in ("queued", "running", "succeeded", "failed"):
        JOBS.labels(status).set(counts.get(status, 0))
    OLDEST.set(float(age))
    return Response(generate_latest(registry), media_type=CONTENT_TYPE_LATEST)
