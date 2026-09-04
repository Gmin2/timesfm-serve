from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from starlette.responses import Response

REQUESTS = Counter("http_requests_total", "http requests", ["method", "path", "status"])
LATENCY = Histogram("http_request_seconds", "http request latency", ["method", "path"], buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10))
FORECAST_SECONDS = Histogram("forecast_seconds", "model predict latency", ["mode"], buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30))
FORECAST_SERIES = Counter("forecast_series_total", "series forecast", ["mode"])
QUEUE_DEPTH = Gauge("forecast_queue_depth", "jobs waiting in the forecast queue")
MODEL_INFO = Gauge("model_info", "loaded model", ["model", "device"])


def render(queue_len_fn):
    QUEUE_DEPTH.set(queue_len_fn())
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
