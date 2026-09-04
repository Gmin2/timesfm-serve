import numpy as np
import pytest
from fastapi.testclient import TestClient

from timesfm_serve import api


class FakeOut:
    def __init__(self, h):
        self.forecast = np.arange(h, dtype=np.float32)
        self.quantiles = np.tile(np.arange(9, dtype=np.float32), (h, 1))


class FakeModel:
    def predict(self, ctx, horizon, past_only_covariates=None, past_future_covariates=None, return_quantiles=False, **kw):
        self.last = (len(ctx), past_only_covariates, past_future_covariates)
        return FakeOut(horizon)


@pytest.fixture
def client(monkeypatch):
    api.state["model"] = FakeModel()
    monkeypatch.setattr(api.db, "tenant_for_key", lambda k: "t1" if k == "good" else None)
    monkeypatch.setattr(api.db, "record_run", lambda *a, **k: None)
    return TestClient(api.app)  # no lifespan, so no real model load


def test_health(client):
    assert client.get("/health").json()["model_loaded"] is True


def test_forecast_needs_key(client):
    assert client.post("/forecast", json={"series": [1] * 10}).status_code == 401
    assert client.post("/forecast", json={"series": [1] * 10}, headers={"x-api-key": "bad"}).status_code == 401


def test_forecast_shapes(client):
    r = client.post("/forecast", json={"series": [1] * 10, "horizon": 5, "future_covariates": [[0] * 15]}, headers={"x-api-key": "good"})
    assert r.status_code == 200
    body = r.json()
    assert len(body["forecast"]) == 5 and len(body["quantiles"]) == 5 and len(body["quantiles"][0]) == 9
    assert api.state["model"].last[2].shape == (1, 15)


def test_short_series_rejected(client):
    assert client.post("/forecast", json={"series": [1, 2]}, headers={"x-api-key": "good"}).status_code == 422


def test_bad_job_id(client):
    assert client.get("/jobs/nope", headers={"x-api-key": "good"}).status_code == 422


def test_metrics_endpoint(client, monkeypatch):
    monkeypatch.setattr(api.jobs, "queue", lambda: [])
    r = client.get("/metrics")
    assert r.status_code == 200 and b"http_requests_total" in r.content
