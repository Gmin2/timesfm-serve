"""Live ingestion, clock policy, and shared delivery against real PostgreSQL."""

import copy
import hashlib
import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
import requests
from fastapi.testclient import TestClient

from scripts import weather_live_ingest
from timesfm_serve import db, weather_api, weather_ingest, weather_live_policy, weather_live_store, weather_store, weather_worker
from timesfm_serve.weather_live_policy import STATIONS, LiveInputError, current_origin, input_digest

ORIGIN = datetime(2026, 9, 5, 12, tzinfo=timezone.utc)
NOW = ORIGIN + timedelta(hours=2)
STATION = "43128599999"


class FixtureClient:
    def __init__(self):
        self.calls = 0

    def fetch(self, provider, url, params):
        self.calls += 1
        station = STATIONS[STATION]
        if provider == "noaa_awc":
            end = datetime.fromisoformat(params["date"])
            rows = []
            for i in range(49):
                observed = end - timedelta(hours=i)
                rows.append({
                    "icaoId": station["icao"], "lat": station["latitude"], "lon": station["longitude"],
                    "obsTime": int(observed.timestamp()), "receiptTime": (observed + timedelta(minutes=2)).isoformat(),
                    "temp": 25 + observed.hour / 24, "metarType": "METAR", "qcField": 16,
                })
            body = json.dumps(rows)
        else:
            run = datetime.fromisoformat(params["run"]).replace(tzinfo=timezone.utc)
            body = json.dumps({
                "latitude": station["latitude"], "longitude": station["longitude"], "elevation": station["elevation_m"],
                "utc_offset_seconds": 0, "hourly_units": {"temperature_2m": "\N{DEGREE SIGN}C"},
                "hourly": {"time": [(run + timedelta(hours=i)).isoformat() for i in range(55)], "temperature_2m": [27.0] * 55},
            })
        return {
            "provider": provider, "url": requests.Request("GET", url, params=params).prepare().url,
            "parameters": params, "received_at": NOW.isoformat(), "body": body,
            "sha256": hashlib.sha256(body.encode()).hexdigest(),
        }


@pytest.fixture(scope="session", autouse=True)
def live_schema():
    db.migrate()
    yield
    db.pool.close()


@pytest.fixture(autouse=True)
def clean_live_state(monkeypatch):
    def clean():
        with db.conn() as c:
            assert c.info.dbname.endswith("_test"), "Live tests require a disposable *_test database"
            c.execute("delete from weather_jobs where kind = 'live'")
            c.execute("delete from weather_live_inputs")
            c.execute("delete from weather_ingestion_status")

    clean()
    monkeypatch.setattr(weather_live_policy, "database_now", lambda c: NOW)
    monkeypatch.setattr(weather_live_policy, "utc_now", lambda: NOW)
    monkeypatch.setattr(weather_ingest, "utc_now", lambda: NOW)
    monkeypatch.setattr(weather_live_ingest, "utc_now", lambda: NOW)
    yield
    clean()


@pytest.fixture
def snapshot():
    client = FixtureClient()
    result = weather_ingest.capture(STATION, ORIGIN, client)
    assert client.calls == 9
    return result


@pytest.fixture
def customer():
    account = db.find_or_create_account(f"live-customer-{uuid.uuid4()}")
    key = db.create_key(account)
    yield account, {"x-api-key": key}
    with db.conn() as c:
        c.execute("delete from accounts where id = %s", (account,))


def output(job, snapshot):
    return {
        "job_id": str(job["id"]), "input_snapshot_id": str(job["input_snapshot_id"]),
        "case_id": job["case_id"], "station_id": STATION, "mode": "experimental_live",
        "production_validated": False, "policy": "awc_metar_live_v1",
        "forecast_origin": ORIGIN.isoformat(), "guidance_run": (ORIGIN - timedelta(hours=6)).isoformat(),
        "inputs_captured_at": snapshot["captured_at"], "latest_observation_at": ORIGIN.isoformat(),
        "generated_at": NOW.isoformat(), "fresh_until": (ORIGIN + timedelta(hours=8)).isoformat(),
        "horizon_hours": 48, "interval_hours": 1, "unit": "celsius", "model": "timesfm_nwp",
        "point_estimate": "p50", "quantile_levels": [q / 10 for q in range(1, 10)], "quantiles_calibrated": False,
        "model_provenance": {}, "input_provenance": {"snapshot_sha256": job["manifest_sha256"]},
        "points": [{
            "valid_time": (ORIGIN + timedelta(hours=i)).isoformat(), "temperature_2m": 24,
            "ecmwf_ifs_temperature_2m": 27, "quantiles": list(range(20, 29)),
        } for i in range(1, 49)],
    }


def queued(snapshot):
    job_id, _ = weather_live_store.submit_snapshot(snapshot)
    job = weather_store.claim()
    assert job["id"] == job_id
    return job


def update_body(source, body):
    source["body"] = json.dumps(body)
    source["sha256"] = hashlib.sha256(source["body"].encode()).hexdigest()


def test_capture_reconstruct_and_no_future_observations(snapshot):
    history, guidance = weather_ingest.reconstruct(snapshot)
    assert len(history) == 168 and len(guidance) == 216
    assert history.index[-1].to_pydatetime() == ORIGIN
    assert not history["imputed"].any()
    assert all(s["parameters"]["models"] == "ecmwf_ifs" for s in snapshot["sources"][4:])
    assert snapshot["observation_audit"]["quality_scope"].startswith("structural_checks_only")


@pytest.mark.parametrize("problem", ["body", "endpoint", "parameters", "receipt", "code", "model", "count"])
def test_input_integrity_and_provider_identity(snapshot, problem):
    bad = copy.deepcopy(snapshot)
    if problem == "body":
        bad["sources"][0]["body"] += " "
    elif problem == "endpoint":
        bad["sources"][0]["url"] = "https://example.com/metar"
    elif problem == "parameters":
        bad["sources"][4]["parameters"]["models"] = "best_match"
    elif problem == "receipt":
        bad["sources"][0]["received_at"] = (NOW + timedelta(seconds=1)).isoformat()
    elif problem == "code":
        bad["inference_source_sha256"]["weather_model.py"] = "changed"
    elif problem == "model":
        bad["model_revision"] = "latest"
    else:
        bad["sources"].pop()
    with pytest.raises(ValueError):
        weather_ingest.reconstruct(bad)


@pytest.mark.parametrize("problem", ["station", "location", "future_receipt", "missing_start", "stale_latest", "truncated"])
def test_metar_rejection_policies(snapshot, problem):
    sources = copy.deepcopy(snapshot["sources"][:4])
    rows = json.loads(sources[0]["body"])
    if problem == "station":
        rows[0]["icaoId"] = "VOMM"
    elif problem == "location":
        rows[0]["lat"] = 0
    elif problem == "future_receipt":
        rows[0]["receiptTime"] = (NOW + timedelta(days=1)).isoformat()
    elif problem == "truncated":
        rows = [rows[0]] * 400
    elif problem == "stale_latest":
        rows = rows[2:]
    elif problem == "missing_start":
        first = int((ORIGIN - timedelta(hours=167)).timestamp())
        for source in sources:
            records = [row for row in json.loads(source["body"]) if row["obsTime"] != first]
            update_body(source, records)
    if problem != "missing_start":
        update_body(sources[0], rows)
    with pytest.raises(ValueError):
        weather_ingest.parse_metars(sources, STATIONS[STATION], ORIGIN)


def test_conflicting_hour_is_missing_not_arbitrary(snapshot):
    sources = copy.deepcopy(snapshot["sources"][:4])
    rows = json.loads(sources[0]["body"])
    conflicting = rows[10] | {"temp": 10.0}
    rows.append(conflicting)
    update_body(sources[0], rows)
    history, audit = weather_ingest.parse_metars(sources, STATIONS[STATION], ORIGIN)
    assert history["imputed"].sum() == 1
    assert audit["conflicting_hours"] == 1


def test_null_nwp_is_not_filled(snapshot):
    sources = copy.deepcopy(snapshot["sources"][4:])
    body = json.loads(sources[-1]["body"])
    body["hourly"]["temperature_2m"][-1] = None
    update_body(sources[-1], body)
    bad = snapshot | {"sources": snapshot["sources"][:4] + sources}
    with pytest.raises(ValueError, match="Finite explicit inputs"):
        weather_ingest.reconstruct(bad)


def test_cycle_waits_for_reports_and_refuses_old_origins():
    assert current_origin(ORIGIN + timedelta(minutes=14)) == ORIGIN - timedelta(hours=6)
    assert current_origin(ORIGIN + timedelta(minutes=15)) == ORIGIN
    for time in (ORIGIN, ORIGIN + timedelta(hours=3, seconds=1)):
        with pytest.raises(LiveInputError):
            weather_live_policy.check_issue_window(ORIGIN, time)
    result = weather_live_ingest.run_cycle([STATION], ORIGIN - timedelta(days=1), FixtureClient())
    assert result["status"] == "waiting_for_issue_window"


def test_live_schedule_is_idempotent_and_uncharged(snapshot, customer):
    with ThreadPoolExecutor(max_workers=8) as pool:
        rows = list(pool.map(lambda _: weather_live_store.submit_snapshot(snapshot), range(12)))
    assert len({row[0] for row in rows}) == 1
    assert sum(row[1] for row in rows) == 1
    assert db.usage(customer[0])["credits_used"] == 0
    with db.conn() as c:
        assert c.execute("select count(*) from weather_live_inputs").fetchone()[0] == 1
        assert c.execute("select kind, points, account_id from weather_jobs where kind='live'").fetchone() == ("live", 0, None)


def test_live_admission_rolls_back_when_queue_full(snapshot, monkeypatch):
    monkeypatch.setattr(weather_store, "GLOBAL_PENDING_LIMIT", 0)
    with pytest.raises(weather_store.SubmissionError):
        weather_live_store.submit_snapshot(snapshot)
    with db.conn() as c:
        assert c.execute("select count(*) from weather_live_inputs").fetchone()[0] == 0


def test_snapshot_roundtrip_and_corruption_detected(snapshot):
    job = queued(snapshot)
    loaded = weather_live_store.load_snapshot(job)
    assert input_digest(loaded) == input_digest(snapshot)
    assert len(weather_ingest.reconstruct(loaded)[0]) == 168
    with db.conn() as c:
        c.execute("update weather_live_inputs set document = '{}'::jsonb where id = %s", (job["input_snapshot_id"],))
    with pytest.raises(ValueError, match="integrity"):
        weather_live_store.load_snapshot(job)


def test_shared_read_api_auth_freshness_and_publication(snapshot, customer, monkeypatch):
    client = TestClient(weather_api.app)
    path = f"/v1/weather/stations/{STATION}/latest"
    assert client.get(path).status_code == 401
    assert client.get(path, headers=customer[1]).status_code == 503
    job = queued(snapshot)
    assert weather_store.complete(job, output(job, snapshot))
    first = client.get(path, headers=customer[1])
    assert first.status_code == 200, first.text
    assert first.json()["mode"] == "experimental_live"
    assert first.json()["published_at"] == NOW.isoformat().replace("+00:00", "Z")
    assert first.json()["prospective_from"] == (NOW + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    assert len(first.json()["points"]) == 48
    assert client.get(path, headers=customer[1]).json() == first.json()
    assert client.get(f"/v1/weather/forecasts/{job['id']}", headers=customer[1]).status_code == 404
    assert db.usage(customer[0])["credits_used"] == 0
    monkeypatch.setattr(weather_live_policy, "database_now", lambda c: ORIGIN + timedelta(hours=8))
    stale = client.get(path, headers=customer[1])
    assert stale.status_code == 503
    assert stale.json()["detail"] == "live_forecast_stale"
    assert stale.headers["retry-after"] == "300"


def test_late_publication_cannot_succeed(snapshot, monkeypatch):
    job = queued(snapshot)
    monkeypatch.setattr(weather_live_policy, "database_now", lambda c: ORIGIN + timedelta(hours=3, seconds=1))
    with pytest.raises(ValueError, match="publication deadline"):
        weather_store.complete(job, output(job, snapshot))
    assert weather_live_store.latest(STATION)[0] is None


def test_output_must_match_live_input_snapshot(snapshot):
    job = queued(snapshot)
    bad = output(job, snapshot)
    bad["input_provenance"]["snapshot_sha256"] = "wrong"
    with pytest.raises(ValueError, match="does not match"):
        weather_store.complete(job, bad)


def test_live_worker_failure_does_not_touch_customer_credits(snapshot, customer):
    weather_live_store.submit_snapshot(snapshot)

    class Engine:
        def predict(self, job):
            raise ValueError("unavailable inputs")

    assert weather_worker.process_one(Engine())
    assert weather_live_store.station_status(STATION)["job"]["error_code"] == "invalid_live_inputs"
    assert db.usage(customer[0])["credits_used"] == 0


def test_ingestion_repeat_skips_network_and_handles_rejections(snapshot, monkeypatch):
    weather_live_store.submit_snapshot(snapshot)
    client = FixtureClient()
    result = weather_live_ingest.run_cycle([STATION], ORIGIN, client)
    assert client.calls == 0
    assert result["stations"][0]["status"] == "existing"

    def reject(*args, **kwargs):
        raise LiveInputError("observation_history_incomplete")

    monkeypatch.setattr(weather_live_ingest, "capture", reject)
    result = weather_live_ingest.run_cycle(["42410099999"], ORIGIN, client)
    assert result["stations"][0]["status"] == "data_rejected"
    assert weather_live_store.station_status("42410099999")["ingestion"]["error_code"] == "observation_history_incomplete"


def test_station_lock_prevents_duplicate_fetches():
    with weather_live_store.station_lock(STATION) as first:
        assert first
        with weather_live_store.station_lock(STATION) as second:
            assert not second
    with weather_live_store.station_lock(STATION) as third:
        assert third


@pytest.mark.parametrize("status", [204, 302, 429, 503])
def test_provider_errors_are_not_accepted_or_retried_in_a_loop(monkeypatch, status):
    class Response:
        status_code = status

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    client = weather_ingest.SourceClient()
    calls = []

    def get(*args, **kwargs):
        calls.append(kwargs)
        return Response()

    monkeypatch.setattr(client.session, "get", get)
    try:
        with pytest.raises(weather_ingest.ProviderError):
            client.fetch("noaa_awc", weather_ingest.METAR_URL, {})
        assert len(calls) == 1
        assert calls[0]["timeout"] == (5, 20)
        assert calls[0]["allow_redirects"] is False
    finally:
        client.close()


def test_provider_response_size_is_bounded(monkeypatch):
    class Response:
        status_code = 200
        url = weather_ingest.METAR_URL

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def iter_content(self, size):
            yield b"x" * (1024 * 1024 + 1)

    client = weather_ingest.SourceClient()
    monkeypatch.setattr(client.session, "get", lambda *args, **kwargs: Response())
    try:
        with pytest.raises(weather_ingest.ProviderError, match="response_limit"):
            client.fetch("noaa_awc", weather_ingest.METAR_URL, {})
    finally:
        client.close()


def test_provider_outage_preserves_a_fresh_stored_forecast(snapshot, monkeypatch):
    job = queued(snapshot)
    weather_store.complete(job, output(job, snapshot))
    weather_live_store.record_status(STATION, ORIGIN, "provider_unavailable", "upstream_unavailable")
    status = weather_live_store.station_status(STATION)
    assert status["available"] is True
    assert status["ingestion"]["status"] == "provider_unavailable"
    monkeypatch.setattr(weather_live_policy, "database_now", lambda c: ORIGIN + timedelta(hours=8))
    assert weather_live_store.station_status(STATION)["available"] is False


def test_live_retry_recovers_without_charging(snapshot, customer):
    job = queued(snapshot)
    assert weather_store.fail(job)
    with db.conn() as c:
        c.execute("update weather_jobs set available_at = now() - interval '1 second' where id = %s", (job["id"],))
    resumed = weather_store.claim()
    assert resumed["lease_token"] != job["lease_token"]
    assert not weather_store.complete(job, output(job, snapshot))
    assert weather_store.complete(resumed, output(resumed, snapshot))
    assert db.usage(customer[0])["credits_used"] == 0
