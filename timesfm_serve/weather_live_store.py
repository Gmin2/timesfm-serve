"""Shared live forecasts use the existing durable queue, without customer charges."""

import uuid
from contextlib import contextmanager
from datetime import timedelta

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from timesfm_serve import db, weather_live_policy, weather_store
from timesfm_serve.weather_live_policy import MAX_ISSUE_DELAY, POLICY, SERVE_MAX_AGE, STATIONS, check_issue_window, input_digest, parse_utc, utc_now


def existing_job(station_id, origin):
    with db.conn() as c:
        row = c.execute(
            "select j.id from weather_live_inputs i join weather_jobs j on j.input_snapshot_id = i.id"
            " where i.station_id = %s and i.forecast_origin = %s and j.kind = 'live'", (station_id, origin),
        ).fetchone()
        return row[0] if row else None


@contextmanager
def station_lock(station_id):
    if station_id not in STATIONS:
        raise ValueError("unknown_station")
    # Session lock, not a transaction: provider calls do not hold an open DB transaction.
    with db.conn() as c:
        acquired = c.execute("select pg_try_advisory_lock(%s)", (int(station_id),)).fetchone()[0]
        try:
            yield acquired
        finally:
            if acquired:
                c.execute("select pg_advisory_unlock(%s)", (int(station_id),))


def submit_snapshot(document):
    station_id, origin = document["station_id"], parse_utc(document["forecast_origin"])
    captured_at = parse_utc(document["captured_at"])
    check_issue_window(origin, captured_at)
    digest = input_digest(document)
    if station_id not in STATIONS or document["policy"] != POLICY:
        raise ValueError("invalid_live_identity")
    with db.conn() as c, c.transaction():
        c.execute("select pg_advisory_xact_lock(%s)", (weather_store.QUEUE_LOCK,))
        row = c.execute(
            "select j.id from weather_live_inputs i join weather_jobs j on j.input_snapshot_id = i.id"
            " where i.station_id = %s and i.forecast_origin = %s", (station_id, origin),
        ).fetchone()
        if row:
            return row[0], False
        now = weather_live_policy.database_now(c)
        check_issue_window(origin, now)
        if captured_at > now:
            raise ValueError("capture_time_in_future")
        count = c.execute("select count(*) from weather_jobs where status in ('queued', 'running')").fetchone()[0]
        if count >= weather_store.GLOBAL_PENDING_LIMIT:
            raise weather_store.SubmissionError("queue_full", 503)
        snapshot_id, job_id = uuid.uuid4(), uuid.uuid4()
        c.execute(
            "insert into weather_live_inputs (id, station_id, forecast_origin, captured_at, issue_deadline, document, sha256)"
            " values (%s, %s, %s, %s, %s, %s, %s)",
            (snapshot_id, station_id, origin, captured_at, origin + MAX_ISSUE_DELAY, Jsonb(document), digest),
        )
        c.execute(
            "insert into weather_jobs (id, account_id, idempotency_key, case_id, manifest_sha256, kind, input_snapshot_id, points)"
            " values (%s, null, %s, %s, %s, 'live', %s, 0)",
            (job_id, str(snapshot_id), f"{station_id}_{origin.strftime('%Y%m%dT%H%MZ')}", digest, snapshot_id),
        )
        return job_id, True


def load_snapshot(job):
    with db.conn() as c:
        row = c.execute("select document, sha256 from weather_live_inputs where id = %s", (job["input_snapshot_id"],)).fetchone()
    if row is None or row[1] != job["manifest_sha256"] or input_digest(row[0]) != row[1]:
        raise ValueError("live_snapshot_integrity_failure")
    return row[0]


def record_status(station_id, origin, status, error_code=None):
    with db.conn() as c:
        c.execute(
            "insert into weather_ingestion_status (station_id, forecast_origin, status, error_code) values (%s, %s, %s, %s)"
            " on conflict (station_id) do update set forecast_origin = excluded.forecast_origin,"
            " attempted_at = clock_timestamp(), status = excluded.status, error_code = excluded.error_code",
            (station_id, origin, status, error_code),
        )


def latest(station_id):
    with db.conn() as c:
        row = c.execute(
            "select f.document, i.forecast_origin, f.published_at from weather_forecasts f"
            " join weather_jobs j on j.id = f.job_id join weather_live_inputs i on i.id = j.input_snapshot_id"
            " where j.kind = 'live' and j.status = 'succeeded' and i.station_id = %s"
            " order by i.forecast_origin desc limit 1", (station_id,),
        ).fetchone()
        now = weather_live_policy.database_now(c)
    if not row:
        return None, "live_forecast_unavailable"
    document, origin, published = row
    if now >= origin + SERVE_MAX_AGE or parse_utc(document["generated_at"]) > now or published > now:
        return None, "live_forecast_stale"
    document["published_at"] = published.isoformat()
    document["prospective_from"] = (published.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)).isoformat()
    return document, None


def station_status(station_id):
    with db.conn() as c, c.cursor(row_factory=dict_row) as cur:
        ingestion = cur.execute("select forecast_origin, attempted_at, status, error_code from weather_ingestion_status where station_id = %s", (station_id,)).fetchone()
        job = cur.execute(
            "select j.status, j.attempts, j.error_code, i.forecast_origin from weather_jobs j"
            " join weather_live_inputs i on i.id = j.input_snapshot_id"
            " where j.kind = 'live' and i.station_id = %s order by i.forecast_origin desc limit 1", (station_id,),
        ).fetchone()
    forecast, reason = latest(station_id)
    return {
        "station_id": station_id, "mode": "experimental_live", "checked_at": utc_now(),
        "available": forecast is not None, "unavailable_reason": reason,
        "forecast_origin": forecast["forecast_origin"] if forecast else None,
        "fresh_until": forecast["fresh_until"] if forecast else None,
        "ingestion": ingestion, "job": job,
    }
