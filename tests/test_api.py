"""api tests against a real postgres.

the parts worth testing here are the sql: that concurrent reservations cannot
oversell a grant, that a failed forecast refunds, that a revoked key stops
working. mocking the database would test none of that.
"""

import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import numpy as np
import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("DATABASE_URL", "postgresql://tfm:tfm@localhost:5432/tfm_test")

from timesfm_serve import api, db  # noqa: E402
from timesfm_serve.auth import hash_key  # noqa: E402

SERIES = [1000.0 + i for i in range(20)]


class FakeOut:
    def __init__(self, horizon):
        self.forecast = np.arange(horizon, dtype=np.float32)
        self.quantiles = np.tile(np.arange(9, dtype=np.float32), (horizon, 1))


class FakeModel:
    def __init__(self, fail=False):
        self.fail = fail

    def predict(self, ctx, horizon, **kw):
        if self.fail:
            raise RuntimeError("model exploded")
        return FakeOut(horizon)


@pytest.fixture(scope="session", autouse=True)
def schema():
    db.migrate()
    yield
    # otherwise the pool's threads outlive the interpreter and the run ends in
    # a PythonFinalizationError that looks like a failure in ci
    db.pool.close()


@pytest.fixture
def account():
    with db.conn() as c:
        c.execute("delete from forecast_runs")
        c.execute("delete from rate_limit_counters")
        c.execute("delete from job_results")
        c.execute("delete from jobs")
        c.execute("delete from api_keys")
        c.execute("delete from sessions")
        c.execute("delete from accounts")
    return db.find_or_create_account(f"acct-{uuid.uuid4()}")


@pytest.fixture
def key(account):
    return db.create_key(account, "test")


@pytest.fixture
def client():
    api.state["model"] = FakeModel()
    # no lifespan, so the model is injected rather than loaded
    return TestClient(api.app)


def granted(account_id, granted=None, used=None, rate=None):
    sets, args = [], []
    for col, val in (("credits_granted", granted), ("credits_used", used), ("rate_limit_per_min", rate)):
        if val is not None:
            sets.append(f"{col} = %s")
            args.append(val)
    with db.conn() as c:
        c.execute(f"update accounts set {', '.join(sets)} where id = %s", (*args, account_id))


def credits_used(account_id):
    with db.conn() as c:
        return c.execute("select credits_used from accounts where id = %s", (account_id,)).fetchone()[0]


# ------------------------------------------------------------------ credits


def test_reserve_refuses_to_pass_the_grant(account):
    granted(account, granted=30, used=0)
    assert db.reserve_credits(account, 20) == 10
    assert db.reserve_credits(account, 20) is None
    assert db.reserve_credits(account, 10) == 0


def test_reserve_never_oversells_under_concurrency(account):
    granted(account, granted=100, used=0)
    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(lambda _: db.reserve_credits(account, 10), range(20)))
    assert sum(r is not None for r in results) == 10
    assert credits_used(account) == 100


def test_refund_never_goes_below_zero(account):
    granted(account, used=5)
    db.refund_credits(account, 50)
    assert credits_used(account) == 0


def test_suspended_account_cannot_spend(account):
    with db.conn() as c:
        c.execute("update accounts set suspended_at = now() where id = %s", (account,))
    assert db.reserve_credits(account, 1) is None


# --------------------------------------------------------------------- auth


def test_missing_key_is_rejected(client):
    assert client.post("/forecast", json={"series": SERIES}).status_code == 401


def test_unknown_key_is_rejected(client):
    r = client.post("/forecast", json={"series": SERIES}, headers={"x-api-key": "tfm_nope"})
    assert r.status_code == 401


def test_revoked_key_stops_working(client, account, key):
    with db.conn() as c:
        c.execute("update api_keys set revoked_at = now() where account_id = %s", (account,))
    r = client.post("/forecast", json={"series": SERIES}, headers={"x-api-key": key})
    assert r.status_code == 401


def test_the_key_itself_is_never_stored(account, key):
    with db.conn() as c:
        row = c.execute("select hash, prefix from api_keys where account_id = %s", (account,)).fetchone()
    assert row[0] != key
    assert len(row[0]) == 64
    assert row[0] == hash_key(key)
    assert key.startswith(row[1])


# ----------------------------------------------------------------- forecast


def test_forecast_charges_and_logs_a_run(client, account, key):
    r = client.post("/forecast", json={"series": SERIES, "horizon": 14}, headers={"x-api-key": key})
    assert r.status_code == 200
    body = r.json()
    assert len(body["forecast"]) == 14
    assert body["credits_charged"] == 14
    assert r.headers["x-credits-remaining"] == str(50000 - 14)
    assert r.headers["x-request-id"]

    with db.conn() as c:
        row = c.execute(
            "select points, context_len from forecast_runs where account_id = %s", (account,)
        ).fetchone()
    assert row == (14, 20)


def test_failed_inference_refunds(client, account, key):
    api.state["model"] = FakeModel(fail=True)
    with pytest.raises(RuntimeError):
        client.post("/forecast", json={"series": SERIES, "horizon": 14}, headers={"x-api-key": key})
    assert credits_used(account) == 0


def test_out_of_credits_is_402(client, account, key):
    granted(account, granted=5)
    r = client.post("/forecast", json={"series": SERIES, "horizon": 14}, headers={"x-api-key": key})
    assert r.status_code == 402
    assert r.json()["credits_needed"] == 14


def test_rate_limit_throttles_and_does_not_charge(client, account, key, monkeypatch):
    # the limiter buckets by wall clock, so without pinning now() this test
    # resets its own window whenever it straddles a minute boundary
    fixed = datetime(2026, 1, 1, 0, 0, 10, tzinfo=timezone.utc)

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed

    monkeypatch.setattr(db, "datetime", FrozenDatetime)
    granted(account, rate=2, used=0)
    codes = [
        client.post("/forecast", json={"series": SERIES, "horizon": 1}, headers={"x-api-key": key}).status_code
        for _ in range(4)
    ]
    assert codes == [200, 200, 429, 429]
    assert credits_used(account) == 2


def test_covariates_of_the_wrong_length_are_rejected(client, key):
    r = client.post(
        "/forecast",
        json={"series": SERIES, "horizon": 7, "future_covariates": [[1.0] * 5]},
        headers={"x-api-key": key},
    )
    assert r.status_code == 422
    assert "27 points" in r.json()["error"]


def test_short_series_rejected(client, key):
    r = client.post("/forecast", json={"series": [1, 2, 3]}, headers={"x-api-key": key})
    assert r.status_code == 422


def test_bad_job_id(client, key):
    assert client.get("/jobs/nope", headers={"x-api-key": key}).status_code == 422


# -------------------------------------------------------------------- misc


def test_usage_reports_the_balance(client, account, key):
    granted(account, granted=100, used=25)
    body = client.get("/usage", headers={"x-api-key": key}).json()
    assert body["credits_remaining"] == 75


def test_health(client):
    assert client.get("/health").json()["model_loaded"] is True


def test_metrics(client):
    r = client.get("/metrics")
    assert r.status_code == 200
    assert b"http_requests_total" in r.content


def test_dashboard_needs_a_session(client):
    assert client.get("/dash/me").status_code == 401
