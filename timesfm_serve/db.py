import os
import secrets
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

DSN = os.environ.get("DATABASE_URL", "postgresql://tfm:tfm@localhost:5432/tfm")
MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"

# arbitrary but fixed, so every process takes the same lock
MIGRATION_LOCK = 8_274_119

pool = ConnectionPool(DSN, min_size=1, max_size=int(os.environ.get("DB_POOL_MAX", "10")), open=False)


def open_pool():
    if pool.closed:
        pool.open()
    pool.wait(timeout=30)


@contextmanager
def conn():
    with pool.connection() as c:
        c.autocommit = True
        yield c


def migrate():
    """apply migrations/*.sql in order, once.

    the whole run is one transaction holding a transaction scoped advisory
    lock. several processes boot at once here (the api and the worker), and
    even "create table if not exists" races in postgres, so the lock has to
    cover the ddl too. a transaction scoped lock rather than a session one so
    this still works behind a pooler in transaction mode.
    """
    open_pool()
    with pool.connection() as c:
        c.autocommit = False
        try:
            c.execute("select pg_advisory_xact_lock(%s)", (MIGRATION_LOCK,))
            c.execute(
                "create table if not exists schema_migrations ("
                " name text primary key,"
                " applied_at timestamptz not null default now())"
            )
            applied = {r[0] for r in c.execute("select name from schema_migrations").fetchall()}

            for path in sorted(MIGRATIONS.glob("*.sql")):
                if path.name in applied:
                    continue
                c.execute(path.read_text())
                c.execute("insert into schema_migrations (name) values (%s)", (path.name,))
                print(f'{{"level": "info", "msg": "migration applied", "name": "{path.name}"}}')
            c.commit()
        except Exception:
            c.rollback()
            raise


# ---------------------------------------------------------------- accounts


def find_or_create_account(name: str, email: str | None = None, github_id: str | None = None) -> int:
    with conn() as c:
        row = c.execute(
            "insert into accounts (name, email, github_id) values (%s, %s, %s)"
            " on conflict (name) do update set name = excluded.name"
            " returning id",
            (name, email, github_id),
        ).fetchone()
    return row[0]


def account_by_github(github_id: str) -> int | None:
    with conn() as c:
        row = c.execute("select id from accounts where github_id = %s", (github_id,)).fetchone()
    return row[0] if row else None


def create_account_for_github(github_id: str, name: str, email: str | None) -> int:
    with conn() as c:
        row = c.execute(
            "insert into accounts (name, email, github_id, credits_granted) values (%s, %s, %s, %s)"
            " on conflict (github_id) do update set email = excluded.email"
            " returning id",
            (name, email, github_id, int(os.environ.get("SIGNUP_CREDITS", "50000"))),
        ).fetchone()
    return row[0]


def usage(account_id: int) -> dict | None:
    with conn() as c:
        row = c.execute(
            "select credits_granted, credits_used, rate_limit_per_min, name, email"
            " from accounts where id = %s",
            (account_id,),
        ).fetchone()
    if row is None:
        return None
    granted, used, rate, name, email = row
    return {
        "account": name,
        "email": email,
        "credits_granted": granted,
        "credits_used": used,
        "credits_remaining": granted - used,
        "rate_limit_per_min": rate,
    }


def reserve_credits(account_id: int, points: int) -> int | None:
    """charge before the model runs.

    one atomic statement whose where clause is the guard, so concurrent
    requests cannot each pass a check and then all spend. returns the
    remaining balance, or None when there is not enough left.
    """
    with conn() as c:
        row = c.execute(
            "update accounts set credits_used = credits_used + %s"
            " where id = %s and suspended_at is null"
            "   and credits_used + %s <= credits_granted"
            " returning credits_granted - credits_used",
            (points, account_id, points),
        ).fetchone()
    return row[0] if row else None


def refund_credits(account_id: int, points: int) -> None:
    with conn() as c:
        c.execute(
            "update accounts set credits_used = greatest(0, credits_used - %s) where id = %s",
            (points, account_id),
        )


# ---------------------------------------------------------------- api keys


def create_key(account_id: int, label: str | None = None) -> str:
    from timesfm_serve.auth import hash_key

    key = "tfm_" + secrets.token_urlsafe(24)
    with conn() as c:
        c.execute(
            "insert into api_keys (hash, prefix, label, account_id) values (%s, %s, %s, %s)",
            (hash_key(key), key[:10], label, account_id),
        )
    return key


def account_for_key_hash(key_hash: str) -> tuple[int, str, int] | None:
    """(account id, name, rate limit) for a live key on a live account"""
    with conn() as c:
        row = c.execute(
            "select a.id, a.name, a.rate_limit_per_min"
            " from api_keys k join accounts a on a.id = k.account_id"
            " where k.hash = %s and k.revoked_at is null and a.suspended_at is null",
            (key_hash,),
        ).fetchone()
    return row


def list_keys(account_id: int) -> list[dict]:
    with conn() as c:
        rows = c.execute(
            "select id, prefix, label, created_at, revoked_at from api_keys"
            " where account_id = %s order by id desc",
            (account_id,),
        ).fetchall()
    return [
        {
            "id": r[0],
            "prefix": r[1],
            "label": r[2],
            "created_at": r[3].isoformat(),
            "revoked_at": r[4].isoformat() if r[4] else None,
        }
        for r in rows
    ]


def revoke_key(key_id: int, account_id: int) -> bool:
    with conn() as c:
        cur = c.execute(
            "update api_keys set revoked_at = now()"
            " where id = %s and account_id = %s and revoked_at is null",
            (key_id, account_id),
        )
    return cur.rowcount > 0


# ------------------------------------------------------------- rate limits


def bump_rate_limit(key_hash: str, window_ms: int) -> tuple[int, datetime]:
    """fixed window counter. returns the count in this window and its start."""
    now = datetime.now(timezone.utc)
    epoch_ms = int(now.timestamp() * 1000)
    start = datetime.fromtimestamp((epoch_ms // window_ms) * window_ms / 1000, tz=timezone.utc)
    with conn() as c:
        row = c.execute(
            "insert into rate_limit_counters (key_hash, window_start, count) values (%s, %s, 1)"
            " on conflict (key_hash, window_start)"
            " do update set count = rate_limit_counters.count + 1"
            " returning count",
            (key_hash, start),
        ).fetchone()
    return row[0], start


def sweep_rate_limits() -> None:
    with conn() as c:
        c.execute("delete from rate_limit_counters where window_start < now() - interval '1 hour'")


# ---------------------------------------------------------------- sessions


def create_session(account_id: int, days: int = 30) -> str:
    token = secrets.token_urlsafe(32)
    with conn() as c:
        c.execute(
            "insert into sessions (token, account_id, expires_at) values (%s, %s, %s)",
            (token, account_id, datetime.now(timezone.utc) + timedelta(days=days)),
        )
    return token


def account_for_session(token: str) -> int | None:
    with conn() as c:
        row = c.execute(
            "select account_id from sessions where token = %s and expires_at > now()",
            (token,),
        ).fetchone()
    return row[0] if row else None


def delete_session(token: str) -> None:
    with conn() as c:
        c.execute("delete from sessions where token = %s", (token,))


# ------------------------------------------------------------------- runs


def record_run(
    account_id,
    model,
    horizon,
    context_len,
    n_past,
    n_future,
    points,
    latency_ms,
    request_id=None,
):
    with conn() as c:
        c.execute(
            "insert into forecast_runs (account_id, model, horizon, context_len, n_past_cov,"
            " n_future_cov, points, latency_ms, request_id)"
            " values (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                account_id,
                model,
                horizon,
                context_len,
                n_past,
                n_future,
                points,
                latency_ms,
                request_id,
            ),
        )


# ------------------------------------------------------------------- jobs


def create_job(job_id, account_id, n_series, horizon, points):
    with conn() as c:
        c.execute(
            "insert into jobs (id, account_id, n_series, horizon, points) values (%s, %s, %s, %s, %s)",
            (job_id, account_id, n_series, horizon, points),
        )


def set_job_status(job_id, status, error=None):
    col = {"running": "started_at", "done": "finished_at", "failed": "finished_at"}.get(status)
    stamp = f", {col} = now()" if col else ""
    with conn() as c:
        c.execute(
            f"update jobs set status = %s, error = %s{stamp} where id = %s",
            (status, error, job_id),
        )


def job_account_and_points(job_id) -> tuple[int, int] | None:
    with conn() as c:
        row = c.execute("select account_id, points from jobs where id = %s", (job_id,)).fetchone()
    return row


def save_results(job_id, rows):
    with conn() as c, c.cursor() as cur:
        cur.executemany(
            "insert into job_results (job_id, series_id, forecast, quantiles) values (%s, %s, %s, %s)",
            [(job_id, sid, Jsonb(f), Jsonb(q)) for sid, f, q in rows],
        )


def get_job(job_id, account_id):
    with conn() as c:
        job = c.execute(
            "select status, n_series, horizon, points, error, created_at, started_at, finished_at"
            " from jobs where id = %s and account_id = %s",
            (job_id, account_id),
        ).fetchone()
        if job is None:
            return None
        results = c.execute(
            "select series_id, forecast, quantiles from job_results where job_id = %s",
            (job_id,),
        ).fetchall()
    return job, results


__all__ = ["psycopg", "pool", "conn", "migrate"]
