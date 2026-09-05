import os
import secrets
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from psycopg_pool import ConnectionPool

from timesfm_serve.database_config import database_dsn

DSN = database_dsn()
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
    paths = sorted(MIGRATIONS.glob("*.sql"))
    if not paths:
        raise RuntimeError("No database migrations found; check the application package")
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

            for path in paths:
                if path.name in applied:
                    continue
                c.execute(path.read_text())
                c.execute("insert into schema_migrations (name) values (%s)", (path.name,))
                print(f'{{"level": "info", "msg": "migration applied", "name": "{path.name}"}}')
            c.commit()
        except Exception:
            c.rollback()
            raise


def initialize():
    """Production runtime verifies migrations; only the operator job applies DDL."""
    if os.environ.get("DATABASE_AUTO_MIGRATE", "1") == "1":
        return migrate()
    if os.environ.get("DATABASE_AUTO_MIGRATE") != "0":
        raise ValueError("DATABASE_AUTO_MIGRATE must be 0 or 1")
    expected = {p.name for p in MIGRATIONS.glob("*.sql")}
    if not expected:
        raise RuntimeError("No packaged database migrations")
    open_pool()
    with conn() as c:
        applied = {r[0] for r in c.execute("select name from schema_migrations").fetchall()}
    if not expected <= applied:
        raise RuntimeError("Database migrations are pending; run the operator migration job")


# ---------------------------------------------------------------- accounts


def find_or_create_account(name: str, email: str | None = None) -> int:
    with conn() as c:
        row = c.execute(
            "insert into accounts (name, email) values (%s, %s)"
            " on conflict (name) do update set name = excluded.name"
            " returning id",
            (name, email),
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
