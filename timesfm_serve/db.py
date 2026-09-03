import os
import secrets
from contextlib import contextmanager

import psycopg
from psycopg.types.json import Jsonb

DSN = os.environ.get("DATABASE_URL", "postgresql://tfm:tfm@localhost:5432/tfm")

SCHEMA = """
create table if not exists api_keys (
    key text primary key,
    tenant text not null,
    created_at timestamptz not null default now(),
    disabled boolean not null default false
);
create table if not exists forecast_runs (
    id bigserial primary key,
    tenant text not null,
    model text not null,
    horizon int not null,
    context_len int not null,
    n_past_cov int not null default 0,
    n_future_cov int not null default 0,
    latency_ms real not null,
    created_at timestamptz not null default now()
);
create table if not exists jobs (
    id uuid primary key,
    tenant text not null,
    status text not null default 'queued',
    n_series int not null,
    horizon int not null,
    error text,
    created_at timestamptz not null default now(),
    started_at timestamptz,
    finished_at timestamptz
);
create table if not exists job_results (
    job_id uuid not null references jobs(id) on delete cascade,
    series_id text not null,
    forecast jsonb not null,
    quantiles jsonb not null,
    primary key (job_id, series_id)
);
"""


@contextmanager
def conn():
    with psycopg.connect(DSN, autocommit=True) as c:
        yield c


def init():
    with conn() as c:
        c.execute(SCHEMA)


def create_key(tenant: str) -> str:
    key = "tfm_" + secrets.token_urlsafe(24)
    with conn() as c:
        c.execute("insert into api_keys (key, tenant) values (%s, %s)", (key, tenant))
    return key


def tenant_for_key(key: str) -> str | None:
    with conn() as c:
        row = c.execute("select tenant from api_keys where key = %s and not disabled", (key,)).fetchone()
    return row[0] if row else None


def record_run(tenant, model, horizon, context_len, n_past, n_future, latency_ms):
    with conn() as c:
        c.execute(
            "insert into forecast_runs (tenant, model, horizon, context_len, n_past_cov, n_future_cov, latency_ms)"
            " values (%s, %s, %s, %s, %s, %s, %s)",
            (tenant, model, horizon, context_len, n_past, n_future, latency_ms),
        )


def create_job(job_id, tenant, n_series, horizon):
    with conn() as c:
        c.execute("insert into jobs (id, tenant, n_series, horizon) values (%s, %s, %s, %s)", (job_id, tenant, n_series, horizon))


def set_job_status(job_id, status, error=None):
    col = {"running": "started_at", "done": "finished_at", "failed": "finished_at"}.get(status)
    stamp = f", {col} = now()" if col else ""
    with conn() as c:
        c.execute(f"update jobs set status = %s, error = %s{stamp} where id = %s", (status, error, job_id))


def save_results(job_id, rows):
    with conn() as c:
        with c.cursor() as cur:
            cur.executemany(
                "insert into job_results (job_id, series_id, forecast, quantiles) values (%s, %s, %s, %s)",
                [(job_id, sid, Jsonb(f), Jsonb(q)) for sid, f, q in rows],
            )


def get_job(job_id, tenant):
    with conn() as c:
        job = c.execute(
            "select status, n_series, horizon, error, created_at, started_at, finished_at from jobs where id = %s and tenant = %s",
            (job_id, tenant),
        ).fetchone()
        if job is None:
            return None
        results = c.execute("select series_id, forecast, quantiles from job_results where job_id = %s", (job_id,)).fetchall()
    return job, results
