import os
import secrets
from contextlib import contextmanager

import psycopg

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
