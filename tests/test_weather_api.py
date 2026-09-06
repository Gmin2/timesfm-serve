"""Weather queue semantics against real PostgreSQL; no GPU required in CI."""

import copy
import hashlib
import json
import shutil
import signal
import subprocess
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import psycopg
import pytest
from fastapi.testclient import TestClient

from timesfm_serve import auth, db, weather_api, weather_catalog, weather_store, weather_worker
from timesfm_serve.auth import hash_key
from timesfm_serve.weather_engine import WeatherEngine, read_inputs

CASE = "42410099999_20260818T0600Z"


@pytest.fixture(scope="session", autouse=True)
def weather_schema():
    db.migrate()
    yield
    db.pool.close()


@pytest.fixture
def customer():
    account = db.find_or_create_account(f"weather-test-{uuid.uuid4()}")
    with db.conn() as c:
        c.execute("update accounts set rate_limit_per_min = 10000 where id = %s", (account,))
    key = db.create_key(account)
    yield account, {"x-api-key": key, "idempotency-key": "test-request"}
    with db.conn() as c:
        c.execute("delete from rate_limit_counters where key_hash in (select hash from api_keys where account_id = %s)", (account,))
        c.execute("delete from accounts where id = %s", (account,))


@pytest.fixture
def client():
    return TestClient(weather_api.app)


def submit(account, key="test-request", case=CASE):
    _, _, digest = weather_catalog.case_manifest(case)
    return weather_store.submit(account, key, case, digest)[0]


def document(job):
    origin = datetime(2026, 8, 18, 6, tzinfo=timezone.utc)
    return {
        "job_id": str(job["id"]), "case_id": CASE, "station_id": "42410099999",
        "mode": "historical_replay", "operational_forecast": False,
        "forecast_origin": origin.isoformat(), "guidance_run": (origin - timedelta(hours=6)).isoformat(),
        "generated_at": datetime.now(timezone.utc).isoformat(), "horizon_hours": 48, "interval_hours": 1,
        "unit": "celsius", "model": "timesfm_nwp", "point_estimate": "p50",
        "quantile_levels": [q / 10 for q in range(1, 10)], "quantiles_calibrated": False,
        "model_provenance": {}, "input_provenance": {},
        "points": [{
            "valid_time": (origin + timedelta(hours=i)).isoformat(), "temperature_2m": 24.0,
            "ecmwf_ifs_temperature_2m": 25.0, "quantiles": list(range(20, 29)),
        } for i in range(1, 49)],
    }


def expire(job):
    with db.conn() as c:
        c.execute("update weather_jobs set lease_expires_at = now() - interval '1 second' where id = %s", (job["id"],))


def make_available(job):
    with db.conn() as c:
        c.execute("update weather_jobs set available_at = now() - interval '1 second' where id = %s", (job["id"],))


def test_api_import_does_not_load_ml_libraries():
    result = subprocess.run([
        sys.executable, "-c", "import sys; import timesfm_serve.weather_api; "
        "assert not any(m in sys.modules for m in ('torch', 'timesfm', 'pandas', 'sklearn'))",
    ], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_stations_require_auth_and_expose_replay_mode(client, customer):
    assert client.get("/v1/weather/stations").status_code == 401
    response = client.get("/v1/weather/stations", headers=customer[1])
    assert response.status_code == 200
    assert response.headers["x-request-id"]
    assert response.headers["x-ratelimit-remaining"]
    assert response.headers["cache-control"] == "no-store"
    assert len(response.json()["stations"]) == 3
    assert response.json()["live_forecasts_available"] is False


def test_catalog_lists_only_available_cases(client, customer):
    response = client.get("/v1/weather/replays", headers=customer[1])
    assert response.status_code == 200
    cases = response.json()["cases"]
    assert any(c["case_id"] == CASE for c in cases)
    assert all(len(c["manifest_sha256"]) == 64 for c in cases)


def test_unknown_key_is_rejected(client):
    assert client.get("/v1/weather/stations", headers={"x-api-key": "tfm_nope"}).status_code == 401


def test_revoked_key_stops_working(client, customer):
    account, headers = customer
    assert client.get("/v1/weather/stations", headers=headers).status_code == 200
    key = db.list_keys(account)[0]
    assert db.revoke_key(key["id"], account)
    assert client.get("/v1/weather/stations", headers=headers).status_code == 401


def test_key_revocation_is_scoped_to_its_account(client, customer):
    account, headers = customer
    key = db.list_keys(account)[0]
    assert not db.revoke_key(key["id"], -1)
    assert client.get("/v1/weather/stations", headers=headers).status_code == 200


def test_raw_api_key_is_never_stored(customer):
    account, headers = customer
    key = headers["x-api-key"]
    with db.conn() as c:
        digest, prefix = c.execute("select hash, prefix from api_keys where account_id = %s", (account,)).fetchone()
    assert digest != key
    assert len(digest) == 64
    assert digest == hash_key(key)
    assert key.startswith(prefix)


def test_suspended_account_cannot_access_weather(client, customer):
    account, headers = customer
    with db.conn() as c:
        c.execute("update accounts set suspended_at = now() where id = %s", (account,))
    assert client.get("/v1/weather/stations", headers=headers).status_code == 401
    assert client.post("/v1/weather/replays", json={"case_id": CASE}, headers=headers).status_code == 401
    assert db.usage(account)["credits_used"] == 0


def test_rate_limit_throttles_before_charging(client, customer, monkeypatch):
    fixed = datetime(2026, 1, 1, 0, 0, 10, tzinfo=timezone.utc)

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed

    monkeypatch.setattr(db, "datetime", FrozenDatetime)
    # PostgreSQL's clock is not frozen; random cleanup would remove these old counters.
    monkeypatch.setattr(auth.random, "random", lambda: 1.0)
    account, headers = customer
    with db.conn() as c:
        c.execute("update accounts set rate_limit_per_min = 2 where id = %s", (account,))
    responses = [client.get("/v1/weather/stations", headers=headers) for _ in range(2)]
    responses.append(client.post("/v1/weather/replays", json={"case_id": CASE}, headers=headers))
    assert [r.status_code for r in responses] == [200, 200, 429]
    assert responses[-1].headers["x-ratelimit-remaining"] == "0"
    assert "retry-after" in responses[-1].headers
    assert db.usage(account)["credits_used"] == 0


def test_submit_repeat_conflict_and_no_duplicate_charge(client, customer):
    a, headers = customer
    first = client.post("/v1/weather/replays", json={"case_id": CASE}, headers=headers)
    again = client.post("/v1/weather/replays", json={"case_id": CASE}, headers=headers)
    assert (first.status_code, again.status_code) == (202, 200)
    assert first.json()["job_id"] == again.json()["job_id"]
    conflict = client.post("/v1/weather/replays", json={"case_id": "43279099999_20260818T0600Z"}, headers=headers)
    assert conflict.status_code == 409
    assert db.usage(a)["credits_used"] == 48
    assert client.get(first.headers["location"], headers=headers).json()["status"] == "queued"
    assert client.get(f"/v1/weather/forecasts/{first.json()['job_id']}", headers=headers).status_code == 404


@pytest.mark.parametrize("body", [{"case_id": "../../secret"}, {"case_id": CASE, "horizon": 1000}, {"case_id": "0" * 5000}])
def test_bounded_requests(client, customer, body):
    assert client.post("/v1/weather/replays", json=body, headers=customer[1]).status_code == 422
    assert db.usage(customer[0])["credits_used"] == 0


def test_missing_idempotency_key_and_unavailable_case(client, customer):
    assert client.post("/v1/weather/replays", json={"case_id": CASE}, headers={"x-api-key": customer[1]["x-api-key"]}).status_code == 422
    response = client.post("/v1/weather/replays", json={"case_id": "42410099999_20260101T0600Z"}, headers=customer[1])
    assert response.status_code == 404
    assert db.usage(customer[0])["credits_used"] == 0


def test_concurrent_duplicate_requests_charge_once(customer):
    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(lambda _: submit(customer[0]), range(20)))
    assert len(set(results)) == 1
    assert db.usage(customer[0])["credits_used"] == 48


def test_account_queue_limit_and_idempotent_retry(customer):
    for i in range(3):
        submit(customer[0], str(i))
    with pytest.raises(weather_store.SubmissionError, match="account_queue_full"):
        submit(customer[0], "fourth")
    assert submit(customer[0], "0")
    assert db.usage(customer[0])["credits_used"] == 144


def test_global_queue_limit(client, customer, monkeypatch):
    monkeypatch.setattr(weather_store, "GLOBAL_PENDING_LIMIT", 0)
    result = client.post("/v1/weather/replays", json={"case_id": CASE}, headers=customer[1])
    assert result.status_code == 503
    assert result.headers["retry-after"] == "30"
    assert db.usage(customer[0])["credits_used"] == 0


def test_credit_limit_is_atomic(customer):
    with db.conn() as c:
        c.execute("update accounts set credits_granted = 48 where id = %s", (customer[0],))
    submit(customer[0])
    with pytest.raises(weather_store.SubmissionError, match="insufficient_credits"):
        submit(customer[0], "second")
    assert submit(customer[0])
    assert db.usage(customer[0])["credits_used"] == 48


def test_concurrent_workers_claim_once_and_publish_once(client, customer):
    job_id = submit(customer[0])
    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(lambda _: weather_store.claim(), range(10)))
    jobs = [j for j in results if j]
    assert len(jobs) == 1
    job = jobs[0]
    assert job["id"] == job_id
    assert weather_store.complete(job, document(job))
    assert not weather_store.complete(job, document(job))
    assert not weather_store.fail(job)
    assert db.usage(customer[0])["credits_used"] == 48
    response = client.get(f"/v1/weather/forecasts/{job_id}", headers=customer[1])
    assert response.status_code == 200
    assert response.json()["mode"] == "historical_replay"
    assert len(response.json()["points"]) == 48
    assert response.json()["generated_at"] != response.json()["forecast_origin"]


def test_other_account_cannot_see_job_or_forecast(client, customer):
    job_id = submit(customer[0])
    job = weather_store.claim()
    weather_store.complete(job, document(job))
    other = db.find_or_create_account(f"weather-other-{uuid.uuid4()}")
    try:
        headers = {"x-api-key": db.create_key(other)}
        for path in ("jobs", "forecasts"):
            assert client.get(f"/v1/weather/{path}/{job_id}", headers=headers).status_code == 404
    finally:
        with db.conn() as c:
            c.execute("delete from accounts where id = %s", (other,))


def test_failed_job_refunds_exactly_once(customer):
    job_id = submit(customer[0])
    job = weather_store.claim()
    assert weather_store.fail(job, "invalid_replay", retryable=False)
    assert not weather_store.fail(job, "invalid_replay", retryable=False)
    assert not weather_store.complete(job, document(job))
    assert weather_store.get_job(job_id, customer[0])["refunded"]
    assert db.usage(customer[0])["credits_used"] == 0
    assert submit(customer[0]) == job_id  # Even a terminal retry does not recharge.


def test_expired_worker_cannot_publish_and_new_worker_can(customer):
    submit(customer[0])
    old = weather_store.claim()
    expire(old)
    assert not weather_store.complete(old, document(old))
    assert not weather_store.fail(old)
    assert weather_store.recover_expired() == 1
    assert weather_store.recover_expired() == 0
    assert weather_store.claim() is None  # Retry backoff.
    make_available(old)
    new = weather_store.claim()
    assert new["lease_token"] != old["lease_token"]
    assert not weather_store.complete(old, document(old))
    assert weather_store.complete(new, document(new))
    assert db.usage(customer[0])["credits_used"] == 48


@pytest.mark.parametrize("crash", [False, True])
def test_retries_exhaust_and_refund_once(customer, crash):
    job_id = submit(customer[0])
    for attempt in range(1, 4):
        job = weather_store.claim()
        assert job["attempts"] == attempt
        if crash:
            expire(job)
            assert weather_store.recover_expired() == 1
        else:
            assert weather_store.fail(job)
        if attempt < 3:
            assert db.usage(customer[0])["credits_used"] == 48
            make_available(job)
    assert weather_store.claim() is None
    assert weather_store.recover_expired() == 0
    assert weather_store.get_job(job_id, customer[0])["status"] == "failed"
    assert db.usage(customer[0])["credits_used"] == 0


@pytest.mark.parametrize("failure", ["nonfinite", "unordered", "time_gap", "wrong_job", "wrong_unit"])
def test_invalid_output_is_not_published(customer, failure):
    submit(customer[0])
    job = weather_store.claim()
    bad = copy.deepcopy(document(job))
    if failure == "nonfinite":
        bad["points"][0]["temperature_2m"] = float("nan")
    elif failure == "unordered":
        bad["points"][0]["quantiles"][0] = 100
    elif failure == "time_gap":
        bad["points"][0]["valid_time"] = bad["points"][1]["valid_time"]
    elif failure == "wrong_job":
        bad["job_id"] = str(uuid.uuid4())
    else:
        bad["unit"] = "fahrenheit"
    with pytest.raises(ValueError):
        weather_store.complete(job, bad)
    assert weather_store.get_forecast(job["id"], customer[0]) is None
    assert weather_store.get_job(job["id"], customer[0])["status"] == "running"


def test_publish_transaction_rolls_back_on_db_error(customer, monkeypatch):
    submit(customer[0])
    job = weather_store.claim()
    # A result insert failure must not mark the job successful or lose its reservation.
    with monkeypatch.context() as patch:
        patch.setattr(weather_store, "Jsonb", lambda _: "invalid-json")
        with pytest.raises(psycopg.Error):
            weather_store.complete(job, document(job))
    assert weather_store.get_forecast(job["id"], customer[0]) is None
    assert weather_store.get_job(job["id"], customer[0])["status"] == "running"
    assert db.usage(customer[0])["credits_used"] == 48
    assert weather_store.complete(job, document(job))


def test_worker_processes_then_reads_without_inference(customer):
    submit(customer[0])

    class Engine:
        calls = 0

        def predict(self, job):
            self.calls += 1
            return document(job)

    engine = Engine()
    assert weather_worker.process_one(engine)
    assert not weather_worker.process_one(engine)
    assert engine.calls == 1


def test_worker_invalid_input_is_terminal_and_refunded(customer):
    job_id = submit(customer[0])

    class Engine:
        def predict(self, job):
            raise ValueError("private diagnostic must not enter API errors")

    assert weather_worker.process_one(Engine())
    result = weather_store.get_job(job_id, customer[0])
    assert result["error_code"] == "invalid_replay"
    assert result["status"] == "failed"
    assert db.usage(customer[0])["credits_used"] == 0


def test_readiness_failure_does_not_break_liveness(client, monkeypatch):
    assert client.get("/health/ready").status_code == 200

    def unavailable():
        raise psycopg.OperationalError("private database address")

    monkeypatch.setattr(db, "conn", unavailable)
    result = client.get("/health/ready")
    assert result.status_code == 503
    assert "private" not in result.text
    assert client.get("/health/live").status_code == 200


def test_weather_model_rejects_cpu():
    with pytest.raises(ValueError, match="CPU fallback is disabled"):
        WeatherEngine("cpu")


def test_metrics_are_disabled_or_operator_only(client, customer, monkeypatch):
    monkeypatch.delenv("WEATHER_METRICS_TOKEN", raising=False)
    assert client.get("/metrics", headers=customer[1]).status_code == 404
    monkeypatch.setenv("WEATHER_METRICS_TOKEN", "operator-token")
    assert client.get("/metrics", headers=customer[1]).status_code == 401
    result = client.get("/metrics", headers={"authorization": "Bearer operator-token"})
    assert result.status_code == 200
    assert "weather_http_requests_total" in result.text
    assert "weather_oldest_pending_seconds" in result.text
    assert customer[1]["x-api-key"] not in result.text


def test_oversized_body_is_rejected_before_parsing(client, customer):
    result = client.post("/v1/weather/replays", content=b"x" * 9000, headers=customer[1])
    assert result.status_code == 413
    assert result.headers["x-request-id"]
    assert db.usage(customer[0])["credits_used"] == 0


def test_missing_migrations_fail_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "MIGRATIONS", tmp_path)
    with pytest.raises(RuntimeError, match="No database migrations"):
        db.migrate()


def test_killed_worker_leaves_a_durable_recoverable_job(customer):
    job_id = submit(customer[0])
    crashed = subprocess.run([sys.executable, "-c", """
import os
import signal
from timesfm_serve import db, weather_worker
class CrashingEngine:
    def predict(self, job):
        os.kill(os.getpid(), signal.SIGKILL)
db.open_pool()
weather_worker.process_one(CrashingEngine())
"""], capture_output=True, text=True, timeout=15)
    assert crashed.returncode == -signal.SIGKILL, crashed.stderr
    assert weather_store.get_job(job_id, customer[0])["status"] == "running"
    expire({"id": job_id})
    assert weather_store.recover_expired() == 1
    make_available({"id": job_id})
    resumed = weather_store.claim()
    assert resumed["attempts"] == 2
    assert weather_store.complete(resumed, document(resumed))
    assert db.usage(customer[0])["credits_used"] == 48


def test_real_input_reconstruction():
    _, manifest, digest = weather_catalog.case_manifest(CASE)
    history, guidance, loaded = read_inputs(CASE, digest)
    assert len(history) == 168
    assert len(guidance) == 216
    assert manifest == loaded


def test_tampered_input_and_manifest_rejected(tmp_path, monkeypatch):
    original, _, digest = weather_catalog.case_manifest(CASE)
    target = tmp_path / CASE
    shutil.copytree(original, target)
    monkeypatch.setattr(weather_catalog, "REPLAY_ROOT", tmp_path)
    (target / "guidance.csv").write_bytes(b"tampered")
    with pytest.raises(ValueError, match="input_hash_mismatch"):
        read_inputs(CASE, digest)
    raw = (target / "manifest.json").read_bytes()
    manifest = json.loads(raw)
    manifest["origin"] = "2026-08-19T06:00:00+00:00"
    (target / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="manifest_changed"):
        read_inputs(CASE, digest)
    new_digest = hashlib.sha256((target / "manifest.json").read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="case_identity_mismatch"):
        read_inputs(CASE, new_digest)
