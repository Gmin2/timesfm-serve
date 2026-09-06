"""Persistent customer login and key management. No provider tokens are retained."""

import hashlib
import secrets
from datetime import timedelta

from timesfm_serve import db
from timesfm_serve.auth import hash_key

SESSION_SECONDS = 7 * 24 * 60 * 60
MAX_ACTIVE_KEYS = 3


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def csrf_token(session_token: str) -> str:
    return digest("dashboard-csrf:" + session_token)


def save_attempt(state: str, browser: str, verifier: str) -> None:
    with db.conn() as c, c.transaction():
        c.execute("delete from github_oauth_attempts where expires_at < now()")
        c.execute(
            "insert into github_oauth_attempts values (%s, %s, %s, now() + interval '10 minutes')",
            (digest(state), digest(browser), verifier),
        )


def consume_attempt(state: str, browser: str) -> str | None:
    with db.conn() as c:
        row = c.execute(
            "delete from github_oauth_attempts where state_hash = %s and browser_hash = %s"
            " and expires_at > now() returning code_verifier",
            (digest(state), digest(browser)),
        ).fetchone()
    return row[0] if row else None


def github_session(github_id: int, login: str, previous: str | None = None) -> str | None:
    token = secrets.token_urlsafe(32)
    with db.conn() as c, c.transaction():
        # GitHub's stable numeric ID is the identity, never an email or mutable handle.
        row = c.execute(
            "insert into accounts (name, github_id, github_login, credits_granted) values (%s, %s, %s, 0)"
            " on conflict (github_id) do update set github_login = excluded.github_login"
            " returning id, suspended_at",
            ("github-" + secrets.token_hex(16), str(github_id), login),
        ).fetchone()
        if row[1] is not None:
            return None
        account_id = row[0]
        c.execute("delete from dashboard_sessions where expires_at < now()")
        if previous:
            c.execute("delete from dashboard_sessions where token_hash = %s", (digest(previous),))
        c.execute(
            "delete from dashboard_sessions where token_hash in ("
            "select token_hash from dashboard_sessions where account_id = %s order by created_at desc offset 4)",
            (account_id,),
        )
        c.execute(
            "insert into dashboard_sessions (token_hash, account_id, expires_at) values (%s, %s, now() + %s)",
            (digest(token), account_id, timedelta(seconds=SESSION_SECONDS)),
        )
    return token


def session_account(token: str) -> dict | None:
    with db.conn() as c:
        row = c.execute(
            "select a.id, a.github_id, a.github_login, a.rate_limit_per_min, s.expires_at"
            " from dashboard_sessions s join accounts a on a.id = s.account_id"
            " where s.token_hash = %s and s.expires_at > now() and a.suspended_at is null",
            (digest(token),),
        ).fetchone()
    return {"id": row[0], "github_id": row[1], "login": row[2], "rate_limit_per_min": row[3], "expires_at": row[4]} if row else None


def logout(token: str) -> None:
    with db.conn() as c:
        c.execute("delete from dashboard_sessions where token_hash = %s", (digest(token),))


def keys(account_id: int) -> list[dict]:
    with db.conn() as c:
        rows = c.execute(
            "select id, prefix, label, created_at, revoked_at, read_only from api_keys"
            " where account_id = %s order by id desc limit 100", (account_id,),
        ).fetchall()
    return [{"id": r[0], "prefix": r[1], "label": r[2], "created_at": r[3], "revoked_at": r[4],
             "scope": "weather:read" if r[5] else "weather:read weather:replay"} for r in rows]


def create_customer_key(account_id: int, label: str) -> dict:
    key = "tfm_" + secrets.token_urlsafe(24)
    with db.conn() as c, c.transaction():
        row = c.execute("select suspended_at from accounts where id = %s for update", (account_id,)).fetchone()
        if row is None or row[0] is not None:
            raise ValueError("account_unavailable")
        active, recent = c.execute(
            "select count(*) filter (where revoked_at is null),"
            " count(*) filter (where created_at > now() - interval '1 day') from api_keys where account_id = %s",
            (account_id,),
        ).fetchone()
        if active >= MAX_ACTIVE_KEYS:
            raise ValueError("active_key_limit")
        if recent >= 20:
            raise ValueError("daily_key_limit")
        row = c.execute(
            "insert into api_keys (hash, prefix, label, account_id, read_only) values (%s, %s, %s, %s, true)"
            " returning id, prefix, label, created_at",
            (hash_key(key), key[:10], label, account_id),
        ).fetchone()
    return {"id": row[0], "prefix": row[1], "label": row[2], "created_at": row[3], "scope": "weather:read", "key": key}
