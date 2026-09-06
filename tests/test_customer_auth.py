"""Customer auth uses real PostgreSQL; GitHub is simulated only at the HTTP boundary."""

import hashlib
import json
import uuid
from base64 import urlsafe_b64encode
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, urlsplit

import pytest
import requests
from fastapi.testclient import TestClient

from timesfm_serve import customer_auth as auth
from timesfm_serve import customer_store as store
from timesfm_serve import db, weather_api
from timesfm_serve.auth import hash_key


@pytest.fixture(scope="session", autouse=True)
def schema():
    db.migrate()
    yield
    db.pool.close()


@pytest.fixture
def identity():
    github_id = uuid.uuid4().int % 10**15 + 1
    yield github_id
    with db.conn() as c:
        c.execute("delete from accounts where github_id = %s", (str(github_id),))


@pytest.fixture
def configured(tmp_path, monkeypatch):
    secret = tmp_path / "github-secret"
    secret.write_text("test-only-client-secret")
    monkeypatch.setenv("GITHUB_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("GITHUB_CLIENT_SECRET_FILE", str(secret))
    monkeypatch.setenv("WEATHER_DASHBOARD_ORIGIN", "http://127.0.0.1:5178")
    monkeypatch.setenv("WEATHER_PUBLIC_API_ORIGIN", "http://127.0.0.1:18002")
    return auth.settings()


@pytest.fixture
def client():
    return TestClient(weather_api.app, base_url="http://127.0.0.1:18002", follow_redirects=False)


def signed_in(client, identity, configured):
    token = store.github_session(identity, "example")
    client.cookies.set(configured.session_cookie, token)
    return {"origin": configured.dashboard_origin, "x-csrf-token": store.csrf_token(token)}


def test_oauth_attempt_is_browser_bound_expiring_and_single_use():
    state, browser, verifier = (str(uuid.uuid4()) for _ in range(3))
    store.save_attempt(state, browser, verifier)
    assert store.consume_attempt(state, "different-browser") is None
    assert store.consume_attempt(state, browser) == verifier
    assert store.consume_attempt(state, browser) is None
    store.save_attempt(state, browser, verifier)
    with db.conn() as c:
        c.execute("update github_oauth_attempts set expires_at = now() - interval '1 second' where state_hash = %s", (store.digest(state),))
    assert store.consume_attempt(state, browser) is None


def test_login_identity_is_stable_and_session_is_hashed(identity):
    first = store.github_session(identity, "first-handle")
    account = store.session_account(first)
    second = store.github_session(identity, "renamed-handle", first)
    assert store.session_account(first) is None
    assert store.session_account(second)["id"] == account["id"]
    assert store.session_account(second)["login"] == "renamed-handle"
    with db.conn() as c:
        token_hash = c.execute("select token_hash from dashboard_sessions where account_id = %s", (account["id"],)).fetchone()[0]
    assert token_hash == store.digest(second)
    assert token_hash != second
    assert db.usage(account["id"])["credits_granted"] == 0
    store.logout(second)
    assert store.session_account(second) is None


def test_expired_or_suspended_session_is_denied(identity):
    token = store.github_session(identity, "example")
    account = store.session_account(token)
    with db.conn() as c:
        c.execute("update dashboard_sessions set expires_at = now() - interval '1 second' where token_hash = %s", (store.digest(token),))
    assert store.session_account(token) is None
    token = store.github_session(identity, "example")
    with db.conn() as c:
        c.execute("update accounts set suspended_at = now() where id = %s", (account["id"],))
    assert store.session_account(token) is None
    assert store.github_session(identity, "example") is None


def test_customer_key_reads_weather_but_cannot_launch_gpu_jobs(identity):
    account = store.session_account(store.github_session(identity, "example"))
    created = store.create_customer_key(account["id"], "My application")
    headers = {"x-api-key": created["key"], "idempotency-key": "self-service-test"}
    client = TestClient(weather_api.app)
    assert client.get("/v1/weather/stations", headers=headers).status_code == 200
    assert client.post("/v1/weather/replays", json={"case_id": "42410099999_20260818T0600Z"}, headers=headers).status_code == 403
    listed = store.keys(account["id"])
    assert listed[0]["scope"] == "weather:read"
    assert "key" not in listed[0] and "hash" not in listed[0]
    assert db.account_for_key_hash(hash_key(created["key"]))[0] == account["id"]
    assert db.revoke_key(created["id"], account["id"])
    assert client.get("/v1/weather/stations", headers=headers).status_code == 401


def test_key_limit_is_atomic(identity):
    account = store.session_account(store.github_session(identity, "example"))

    def create(_):
        try:
            return store.create_customer_key(account["id"], "Concurrent key")
        except ValueError as error:
            return str(error)

    with ThreadPoolExecutor(max_workers=6) as executor:
        results = list(executor.map(create, range(6)))
    assert sum(isinstance(value, dict) for value in results) == 3
    assert results.count("active_key_limit") == 3


def test_unconfigured_login_is_explicit(client, monkeypatch):
    monkeypatch.delenv("GITHUB_CLIENT_ID", raising=False)
    monkeypatch.delenv("GITHUB_CLIENT_SECRET_FILE", raising=False)
    assert client.get("/auth/session").json()["configured"] is False
    assert client.get("/auth/github").status_code == 503


def test_full_oauth_pkce_exchange_and_key_lifecycle(client, identity, configured, monkeypatch):
    start = client.get("/auth/github")
    assert start.status_code == 303
    query = parse_qs(urlsplit(start.headers["location"]).query)
    assert query["code_challenge_method"] == ["S256"]
    assert query["scope"] == ["read:user"]
    assert query["redirect_uri"] == [configured.callback]
    requests_seen = []

    def provider_send(self, request, **kwargs):
        requests_seen.append(request.url)
        if request.url == "https://github.com/login/oauth/access_token":
            data = parse_qs(request.body)
            verifier = data["code_verifier"][0]
            challenge = urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
            assert query["code_challenge"] == [challenge]
            assert data["code"] == ["test-authorization-code"]
            assert data["client_secret"] == ["test-only-client-secret"]
            payload = {"access_token": "provider-token-never-retained", "token_type": "bearer"}
        else:
            assert request.url == "https://api.github.com/user"
            assert request.headers["Authorization"] == "Bearer provider-token-never-retained"
            payload = {"id": identity, "login": "example", "type": "User", "email": None}
        result = requests.Response()
        result.status_code = 200
        result._content = json.dumps(payload).encode()
        result.headers["Content-Type"] = "application/json"
        result.request = request
        return result

    monkeypatch.setattr(requests.Session, "send", provider_send)
    path = "/auth/github/callback?code=test-authorization-code&state=" + query["state"][0]
    callback = client.get(path)
    assert callback.headers["location"] == configured.dashboard_origin + "/api-keys"
    assert "httponly" in callback.headers["set-cookie"].lower()
    assert "samesite=lax" in callback.headers["set-cookie"].lower()
    assert "provider-token" not in callback.text + str(callback.headers)
    session = client.get("/auth/session").json()
    assert session["user"]["login"] == "example"
    headers = {"origin": configured.dashboard_origin, "x-csrf-token": session["csrf_token"]}
    created = client.post("/v1/account/keys", json={"label": "Weather app"}, headers=headers)
    assert created.status_code == 201
    assert created.headers["cache-control"] == "no-store"
    key = created.json()
    assert key["key"].startswith("tfm_")
    assert "key" not in client.get("/v1/account/keys").json()["keys"][0]
    assert client.delete(f"/v1/account/keys/{key['id']}", headers=headers).status_code == 204
    assert client.post("/auth/logout", headers=headers).status_code == 204
    assert client.get("/auth/session").json()["user"] is None
    assert "invalid_login_state" in client.get(path).headers["location"]
    assert len(requests_seen) == 2


def test_state_mismatch_and_provider_error_never_sign_in(client, configured):
    start = client.get("/auth/github")
    query = parse_qs(urlsplit(start.headers["location"]).query)
    result = client.get("/auth/github/callback?state=" + "x" * 43 + "&code=not-real")
    assert "invalid_login_state" in result.headers["location"]
    start = client.get("/auth/github")
    query = parse_qs(urlsplit(start.headers["location"]).query)
    denied = client.get("/auth/github/callback?error=access_denied&state=" + query["state"][0])
    assert "github_access_denied" in denied.headers["location"]
    assert client.get("/auth/session").json()["user"] is None


def test_key_mutations_require_session_origin_and_csrf(client, identity, configured):
    assert client.get("/v1/account/keys").status_code == 401
    headers = signed_in(client, identity, configured)
    path = "/v1/account/keys"
    assert client.post(path, json={"label": "Example"}).status_code == 403
    assert client.post(path, json={"label": "Example"}, headers={**headers, "origin": "https://attacker.test"}).status_code == 403
    assert client.post(path, json={"label": "Example"}, headers={**headers, "x-csrf-token": "wrong"}).status_code == 403
    assert client.post(path, json={"label": "   "}, headers=headers).status_code == 422
    assert client.post(path, json={"label": "Example", "account_id": 1}, headers=headers).status_code == 422
    assert client.post(path, json={"label": "Example"}, headers=headers).status_code == 201
    assert client.post("/auth/logout").status_code == 403


def test_customer_cannot_revoke_another_accounts_key(client, identity, configured):
    headers = signed_in(client, identity, configured)
    other = db.find_or_create_account("other-" + str(uuid.uuid4()))
    try:
        key = store.create_customer_key(other, "Other account")
        assert client.delete(f"/v1/account/keys/{key['id']}", headers=headers).status_code == 404
        assert db.account_for_key_hash(hash_key(key["key"])) is not None
    finally:
        with db.conn() as c:
            c.execute("delete from accounts where id = %s", (other,))


def test_secure_cookie_on_https(client, configured, monkeypatch):
    monkeypatch.setenv("WEATHER_DASHBOARD_ORIGIN", "https://forecast.example.test")
    result = client.get("/auth/github")
    cookie = result.headers["set-cookie"]
    assert "__Host-weather_oauth=" in cookie
    assert "Secure" in cookie and "HttpOnly" in cookie and "SameSite=lax" in cookie
    assert "Domain=" not in cookie


def test_failed_token_exchange_redirects_without_leaking_provider_error(client, configured, monkeypatch):
    start = client.get("/auth/github")
    state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]

    def provider_send(self, request, **kwargs):
        result = requests.Response()
        result.status_code = 200
        result._content = b'{"error":"bad_verification_code","error_description":"private-provider-detail"}'
        result.request = request
        return result

    monkeypatch.setattr(requests.Session, "send", provider_send)
    result = client.get("/auth/github/callback?code=expired-code&state=" + state)
    assert result.status_code == 303
    assert "github_login_failed" in result.headers["location"]
    assert "private-provider-detail" not in result.text + str(result.headers)
    assert client.get("/auth/session").json()["user"] is None
