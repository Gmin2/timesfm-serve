"""PostgreSQL is the durable queue and the result store for weather jobs."""

import uuid

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from timesfm_serve import db, weather_live_policy
from timesfm_serve.weather_contract import ForecastDocument
from timesfm_serve.weather_live_contract import LiveForecastDocument

QUEUE_LOCK = 8_274_120
ACCOUNT_PENDING_LIMIT = 3
GLOBAL_PENDING_LIMIT = 100
LEASE_SECONDS = 120


class SubmissionError(Exception):
    def __init__(self, code, status):
        super().__init__(code)
        self.code, self.status = code, status


def submit(account_id, idempotency_key, case_id, manifest_sha256):
    with db.conn() as c, c.transaction():
        # Serialize admission so concurrent API replicas cannot overfill the queue.
        c.execute("select pg_advisory_xact_lock(%s)", (QUEUE_LOCK,))
        account = c.execute(
            "select credits_granted - credits_used, suspended_at from accounts where id = %s for update",
            (account_id,),
        ).fetchone()
        if account is None or account[1] is not None:
            raise SubmissionError("account_unavailable", 401)
        existing = c.execute(
            "select id, case_id, manifest_sha256 from weather_jobs where account_id = %s and idempotency_key = %s",
            (account_id, idempotency_key),
        ).fetchone()
        if existing:
            if (case_id, manifest_sha256) != existing[1:]:
                raise SubmissionError("idempotency_key_conflict", 409)
            return existing[0], False
        if account[0] < 48:
            raise SubmissionError("insufficient_credits", 402)
        total, owned = c.execute(
            "select count(*), count(*) filter (where account_id = %s)"
            " from weather_jobs where status in ('queued', 'running')", (account_id,),
        ).fetchone()
        if total >= GLOBAL_PENDING_LIMIT:
            raise SubmissionError("queue_full", 503)
        if owned >= ACCOUNT_PENDING_LIMIT:
            raise SubmissionError("account_queue_full", 429)
        job_id = uuid.uuid4()
        c.execute(
            "insert into weather_jobs (id, account_id, idempotency_key, case_id, manifest_sha256)"
            " values (%s, %s, %s, %s, %s)",
            (job_id, account_id, idempotency_key, case_id, manifest_sha256),
        )
        c.execute("update accounts set credits_used = credits_used + 48 where id = %s", (account_id,))
        return job_id, True


def get_job(job_id, account_id):
    with db.conn() as c, c.cursor(row_factory=dict_row) as cur:
        return cur.execute(
            "select id, case_id, status, attempts, points as credits_reserved, refunded, error_code,"
            " created_at, started_at, finished_at from weather_jobs where id = %s and account_id = %s",
            (job_id, account_id),
        ).fetchone()


def get_forecast(job_id, account_id):
    with db.conn() as c:
        row = c.execute(
            "select f.document from weather_forecasts f join weather_jobs j on j.id = f.job_id"
            " where j.id = %s and j.account_id = %s and j.status = 'succeeded'", (job_id, account_id),
        ).fetchone()
    return row[0] if row else None


def claim():
    with db.conn() as c, c.transaction(), c.cursor(row_factory=dict_row) as cur:
        row = cur.execute(
            "select id from weather_jobs where status = 'queued' and available_at <= clock_timestamp()"
            " order by created_at for update skip locked limit 1"
        ).fetchone()
        if row is None:
            return None
        return cur.execute(
            "update weather_jobs set status = 'running', attempts = attempts + 1, lease_token = %s,"
            " lease_expires_at = clock_timestamp() + %s * interval '1 second',"
            " started_at = coalesce(started_at, clock_timestamp()), error_code = null"
            " where id = %s returning *", (uuid.uuid4(), LEASE_SECONDS, row["id"]),
        ).fetchone()


def complete(job, document):
    live = job.get("kind") == "live"
    validated = (LiveForecastDocument if live else ForecastDocument).model_validate(document)
    if validated.job_id != job["id"] or validated.case_id != job["case_id"]:
        raise ValueError("Result belongs to a different job")
    with db.conn() as c, c.transaction():
        if live:
            now = weather_live_policy.database_now(c)
            if validated.input_snapshot_id != job["input_snapshot_id"] or validated.generated_at > now:
                raise ValueError("Invalid live output identity or generation time")
            snapshot = c.execute(
                "select station_id, forecast_origin, issue_deadline, captured_at from weather_live_inputs where id = %s", (job["input_snapshot_id"],),
            ).fetchone()
            if snapshot is None or (validated.station_id, validated.forecast_origin) != snapshot[:2] or now > snapshot[2]:
                raise ValueError("Live forecast missed its publication deadline")
            if validated.inputs_captured_at != snapshot[3] or validated.input_provenance.get("snapshot_sha256") != job["manifest_sha256"]:
                raise ValueError("Live output does not match its input snapshot")
        row = c.execute(
            "update weather_jobs set status = 'succeeded', finished_at = clock_timestamp(),"
            " lease_token = null, lease_expires_at = null"
            " where id = %s and status = 'running' and lease_token = %s"
            " and lease_expires_at > clock_timestamp() returning id", (job["id"], job["lease_token"]),
        ).fetchone()
        if row is None:
            return False
        published_at = weather_live_policy.database_now(c)
        if live and published_at > snapshot[2]:
            raise ValueError("Live publication deadline expired")
        c.execute(
            "insert into weather_forecasts (job_id, document, published_at) values (%s, %s, %s)",
            (job["id"], Jsonb(validated.model_dump(mode="json")), published_at),
        )
        return True


def _fail_locked(c, row, error_code, retryable):
    retry = retryable and row["attempts"] < 3
    c.execute(
        "update weather_jobs set status = %s, lease_token = null, lease_expires_at = null, error_code = %s,"
        " available_at = clock_timestamp() + %s * interval '1 second',"
        " finished_at = case when %s then null else clock_timestamp() end, refunded = %s where id = %s",
        ("queued" if retry else "failed", error_code, 5 * 2 ** row["attempts"], retry, not retry, row["id"]),
    )
    if not retry and row["account_id"] is not None:
        c.execute("update accounts set credits_used = credits_used - %s where id = %s", (row["points"], row["account_id"]))


def fail(job, error_code="inference_failed", retryable=True):
    if error_code not in ("inference_failed", "invalid_replay", "invalid_live_inputs"):
        raise ValueError("Use a public error code, not exception text")
    with db.conn() as c, c.transaction(), c.cursor(row_factory=dict_row) as cur:
        row = cur.execute(
            "select * from weather_jobs where id = %s and status = 'running' and lease_token = %s"
            " and lease_expires_at > clock_timestamp() for update", (job["id"], job["lease_token"]),
        ).fetchone()
        if row is None:
            return False
        _fail_locked(c, row, error_code, retryable)
        return True


def recover_expired():
    with db.conn() as c, c.transaction(), c.cursor(row_factory=dict_row) as cur:
        rows = cur.execute(
            "select * from weather_jobs where status = 'running' and lease_expires_at <= clock_timestamp()"
            " order by lease_expires_at for update skip locked limit 100"
        ).fetchall()
        for row in rows:
            _fail_locked(c, row, "worker_lease_expired", retryable=True)
        return len(rows)
