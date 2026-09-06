"""GitHub OAuth and cookie-authenticated customer endpoints, separate from API keys."""

import os
import re
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import requests
from authlib.integrations.base_client.errors import OAuthError
from authlib.integrations.requests_client import OAuth2Session
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict, Field

from timesfm_serve import customer_store as store
from timesfm_serve import db

router = APIRouter(tags=["Customer access"])
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}")


def validated_origin(value: str) -> str:
    parsed = urlsplit(value)
    local = parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "localhost")
    if (parsed.scheme != "https" and not local) or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Dashboard/API origin must use HTTPS or loopback HTTP")
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise ValueError("Dashboard/API origin must not contain a path, query, or fragment")
    return value.rstrip("/")


@dataclass(frozen=True)
class Settings:
    client_id: str
    client_secret: str = field(repr=False)
    dashboard_origin: str
    api_origin: str

    @property
    def callback(self):
        return self.dashboard_origin + "/api/auth/github/callback"

    @property
    def secure(self):
        return self.dashboard_origin.startswith("https:")

    @property
    def session_cookie(self):
        return "__Host-weather_session" if self.secure else "weather_session"

    @property
    def browser_cookie(self):
        return "__Host-weather_oauth" if self.secure else "weather_oauth"


def settings() -> Settings | None:
    client_id = os.environ.get("GITHUB_CLIENT_ID", "")
    secret_path = os.environ.get("GITHUB_CLIENT_SECRET_FILE", "")
    if not client_id and not secret_path:
        return None
    if not client_id or not secret_path:
        raise ValueError("GitHub login requires GITHUB_CLIENT_ID and GITHUB_CLIENT_SECRET_FILE")
    secret = Path(secret_path).read_text().strip()
    if not secret:
        raise ValueError("GitHub client secret file is empty")
    return Settings(client_id, secret, validated_origin(os.environ["WEATHER_DASHBOARD_ORIGIN"]),
                    validated_origin(os.environ["WEATHER_PUBLIC_API_ORIGIN"]))


def configured() -> Settings:
    config = settings()
    if config is None:
        raise HTTPException(503, "github_login_not_configured")
    return config


def oauth_client(config: Settings) -> OAuth2Session:
    return OAuth2Session(config.client_id, config.client_secret, redirect_uri=config.callback,
                         scope="read:user", code_challenge_method="S256", token_endpoint_auth_method="client_secret_post")


def cookie(response: Response, name: str, value: str, config: Settings, max_age: int):
    response.set_cookie(name, value, max_age=max_age, secure=config.secure, httponly=True, samesite="lax", path="/")


def clear_cookie(response: Response, name: str, config: Settings):
    response.delete_cookie(name, secure=config.secure, httponly=True, samesite="lax", path="/")


def limited(bucket: str, limit: int):
    count, _ = db.bump_rate_limit(bucket, 60_000)
    if count > limit:
        raise HTTPException(429, "rate_limit_exceeded", headers={"retry-after": "60"})


def require_session(request: Request, config: Settings = Depends(configured)) -> dict:
    token = request.cookies.get(config.session_cookie, "")
    account = store.session_account(token) if TOKEN_PATTERN.fullmatch(token) else None
    if account is None:
        raise HTTPException(401, "session_expired")
    if request.method != "GET":
        csrf = request.headers.get("x-csrf-token", "")
        if request.headers.get("origin") != config.dashboard_origin or not secrets.compare_digest(csrf, store.csrf_token(token)):
            raise HTTPException(403, "csrf_validation_failed")
        limited("dashboard:" + str(account["id"]), 20)
    return account


@router.get("/auth/session")
def session(request: Request, response: Response):
    config = settings()
    payload = {"configured": config is not None, "user": None, "csrf_token": None,
               "api_origin": config.api_origin if config else None, "max_active_keys": store.MAX_ACTIVE_KEYS}
    if config:
        token = request.cookies.get(config.session_cookie, "")
        account = store.session_account(token) if TOKEN_PATTERN.fullmatch(token) else None
        if account:
            payload.update(user=account, csrf_token=store.csrf_token(token))
        elif token:
            clear_cookie(response, config.session_cookie, config)
    return payload


@router.get("/auth/github")
def login(config: Settings = Depends(configured)):
    # A shared budget bounds outstanding, anonymous OAuth state across API replicas.
    limited("github-login", 120)
    state, browser, verifier = (secrets.token_urlsafe(32) for _ in range(3))
    with oauth_client(config) as client:
        url, _ = client.create_authorization_url("https://github.com/login/oauth/authorize", state=state, code_verifier=verifier)
    store.save_attempt(state, browser, verifier)
    response = RedirectResponse(url, status_code=303)
    cookie(response, config.browser_cookie, browser, config, 600)
    return response


def github_identity(config: Settings, code: str, verifier: str) -> tuple[int, str]:
    with oauth_client(config) as client:
        client.fetch_token("https://github.com/login/oauth/access_token", code=code, code_verifier=verifier,
                           headers={"Accept": "application/json"}, timeout=10, allow_redirects=False)
        response = client.get("https://api.github.com/user", timeout=10, allow_redirects=False, headers={
            "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "Forecast-Lab",
        })
        response.raise_for_status()
        profile = response.json()
    if (not isinstance(profile, dict) or type(profile.get("id")) is not int or profile["id"] <= 0
            or profile.get("type") != "User" or not isinstance(profile.get("login"), str)
            or not re.fullmatch(r"[A-Za-z0-9-]{1,39}", profile["login"])):
        raise ValueError("invalid_github_profile")
    return profile["id"], profile["login"]


@router.get("/auth/github/callback")
def callback(request: Request, config: Settings = Depends(configured)):
    state = request.query_params.get("state", "")
    browser = request.cookies.get(config.browser_cookie, "")
    verifier = store.consume_attempt(state, browser) if TOKEN_PATTERN.fullmatch(state) and TOKEN_PATTERN.fullmatch(browser) else None
    error, token = "invalid_login_state", None
    if verifier:
        code = request.query_params.get("code", "")
        error = "github_access_denied" if request.query_params.get("error") else "github_login_failed"
        if code and len(code) <= 4096 and not request.query_params.get("error"):
            try:
                github_id, handle = github_identity(config, code, verifier)
                token = store.github_session(github_id, handle, request.cookies.get(config.session_cookie))
                error = "account_suspended" if token is None else ""
            except (requests.RequestException, OAuthError, ValueError, KeyError):
                pass
    target = config.dashboard_origin + "/api-keys" + ("?auth_error=" + error if error else "")
    response = RedirectResponse(target, status_code=303)
    response.headers["referrer-policy"] = "no-referrer"
    clear_cookie(response, config.browser_cookie, config)
    if token:
        cookie(response, config.session_cookie, token, config, store.SESSION_SECONDS)
    return response


@router.post("/auth/logout", status_code=204)
def logout(request: Request, response: Response, account: dict = Depends(require_session), config: Settings = Depends(configured)):
    store.logout(request.cookies[config.session_cookie])
    clear_cookie(response, config.session_cookie, config)


@router.get("/v1/account/keys")
def keys(account: dict = Depends(require_session)):
    return {"keys": store.keys(account["id"])}


class KeyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    label: str = Field(min_length=1, max_length=64, pattern=r"^[^\x00-\x1f\x7f]+$")


@router.post("/v1/account/keys", status_code=201)
def create_key(body: KeyRequest, account: dict = Depends(require_session)):
    try:
        return store.create_customer_key(account["id"], body.label)
    except ValueError as error:
        code = str(error)
        raise HTTPException(401 if code == "account_unavailable" else 409, code) from None


@router.delete("/v1/account/keys/{key_id}", status_code=204)
def revoke_key(key_id: int, account: dict = Depends(require_session)):
    if not db.revoke_key(key_id, account["id"]):
        raise HTTPException(404, "key_not_found")
