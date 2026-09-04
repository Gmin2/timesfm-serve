import os

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from timesfm_serve import db

CLIENT_ID = os.environ.get("GITHUB_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("GITHUB_CLIENT_SECRET", "")
BASE_URL = os.environ.get("APP_BASE_URL", "http://localhost:8000")
COOKIE = "tfm_session"

router = APIRouter()


def configured() -> bool:
    return bool(CLIENT_ID and CLIENT_SECRET)


@router.get("/auth/config")
def auth_config():
    return {
        "github_configured": configured(),
        "signup_credits": int(os.environ.get("SIGNUP_CREDITS", "50000")),
    }


@router.get("/auth/github")
def start_github(request: Request):
    if not configured():
        raise HTTPException(503, "github sign in is not configured on this server")
    # the state cookie is compared on the way back, so a third party cannot
    # start a login on someone else's behalf
    state = os.urandom(16).hex()
    url = (
        "https://github.com/login/oauth/authorize"
        f"?client_id={CLIENT_ID}"
        f"&redirect_uri={BASE_URL}/auth/github/callback"
        f"&scope=read:user user:email&state={state}"
    )
    resp = RedirectResponse(url)
    resp.set_cookie("tfm_oauth_state", state, httponly=True, max_age=600, samesite="lax")
    return resp


@router.get("/auth/github/callback")
async def github_callback(request: Request, code: str = "", state: str = ""):
    if not configured():
        raise HTTPException(503, "github sign in is not configured on this server")
    expected = request.cookies.get("tfm_oauth_state")
    if not code or not state or state != expected:
        raise HTTPException(400, "invalid oauth state")

    async with httpx.AsyncClient(timeout=15) as client:
        token_res = await client.post(
            "https://github.com/login/oauth/access_token",
            headers={"accept": "application/json"},
            data={
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "code": code,
                "redirect_uri": f"{BASE_URL}/auth/github/callback",
            },
        )
        token = token_res.json().get("access_token")
        if not token:
            raise HTTPException(401, "github did not return a token")

        headers = {"authorization": f"Bearer {token}", "accept": "application/vnd.github+json"}
        user = (await client.get("https://api.github.com/user", headers=headers)).json()
        email = user.get("email")
        if not email:
            emails = (await client.get("https://api.github.com/user/emails", headers=headers)).json()
            primary = next((e for e in emails if e.get("primary")), None)
            email = primary.get("email") if primary else None

    github_id = str(user["id"])
    account_id = db.account_by_github(github_id)
    if account_id is None:
        name = email or user.get("login") or f"github-{github_id}"
        account_id = db.create_account_for_github(github_id, name, email)

    token = db.create_session(account_id)
    resp = RedirectResponse("/dashboard")
    resp.set_cookie(COOKIE, token, httponly=True, max_age=30 * 86400, samesite="lax")
    resp.delete_cookie("tfm_oauth_state")
    return resp


@router.post("/auth/signout")
def signout(request: Request):
    token = request.cookies.get(COOKIE)
    if token:
        db.delete_session(token)
    resp = RedirectResponse("/dashboard", status_code=303)
    resp.delete_cookie(COOKIE)
    return resp
