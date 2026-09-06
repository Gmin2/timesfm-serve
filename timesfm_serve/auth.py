import hashlib
import os
import random
import re
import time

from fastapi import Header, HTTPException, Request

from timesfm_serve import db

RATE_LIMIT_WINDOW_MS = int(os.environ.get("RATE_LIMIT_WINDOW_MS", "60000"))
KEY_PATTERN = re.compile(r"tfm_[A-Za-z0-9_-]{32}")


def hash_key(key: str) -> str:
    """api keys are 24 random bytes, so a plain fast hash is the right tool.

    bcrypt and argon2 exist to slow down guessing of low entropy human
    passwords, and would add tens of milliseconds to every request for no gain
    against a key nobody is going to guess.
    """
    return hashlib.sha256(key.encode()).hexdigest()


class Account:
    __slots__ = ("id", "name", "rate_limit_per_min", "key_hash", "read_only")

    def __init__(self, id: int, name: str, rate_limit_per_min: int, key_hash: str, read_only: bool = False):
        self.id = id
        self.name = name
        self.rate_limit_per_min = rate_limit_per_min
        self.key_hash = key_hash
        self.read_only = read_only


def require_key(request: Request, x_api_key: str | None = Header(default=None)) -> Account:
    """authenticate the key, then rate limit it.

    the limiter runs here rather than inside the endpoints so a throttled call
    never reaches the credit reservation and is never charged.
    """
    if not x_api_key:
        raise HTTPException(401, "missing api key")
    if not KEY_PATTERN.fullmatch(x_api_key):
        raise HTTPException(401, "invalid api key")

    key_hash = hash_key(x_api_key)
    row = db.account_for_key_hash(key_hash)
    if row is None:
        raise HTTPException(401, "invalid api key")

    account = Account(row[0], row[1], row[2], key_hash, row[3])

    count, window_start = db.bump_rate_limit(key_hash, RATE_LIMIT_WINDOW_MS)
    window_end = window_start.timestamp() + RATE_LIMIT_WINDOW_MS / 1000
    reset = max(0, int(window_end - time.time()))
    remaining = max(0, account.rate_limit_per_min - count)
    request.state.rate_limit = (account.rate_limit_per_min, remaining, reset)

    if count > account.rate_limit_per_min:
        raise HTTPException(
            429,
            "rate limit exceeded",
            headers={
                "retry-after": str(reset),
                "x-ratelimit-limit": str(account.rate_limit_per_min),
                "x-ratelimit-remaining": "0",
                "x-ratelimit-reset": str(reset),
            },
        )

    # old windows are dead weight, sweep them now and then
    if random.random() < 0.01:
        try:
            db.sweep_rate_limits()
        except Exception:
            pass

    return account
