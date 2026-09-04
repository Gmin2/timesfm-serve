from pathlib import Path

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import HTMLResponse

from timesfm_serve import db
from timesfm_serve.oauth import COOKIE

STATIC = Path(__file__).resolve().parent / "static"

router = APIRouter()


def current_account(request: Request) -> int:
    token = request.cookies.get(COOKIE)
    account_id = db.account_for_session(token) if token else None
    if account_id is None:
        raise HTTPException(401, "not signed in")
    return account_id


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard_page():
    return (STATIC / "dashboard.html").read_text()


@router.get("/dash/me")
def me(request: Request):
    return db.usage(current_account(request))


@router.get("/dash/keys")
def keys(request: Request):
    return {"keys": db.list_keys(current_account(request))}


@router.post("/dash/keys", status_code=201)
def new_key(request: Request, body: dict = Body(default={})):
    label = str(body.get("label") or "")[:60] or None
    key = db.create_key(current_account(request), label)
    # the only time the caller ever sees it, a hash from here on
    return {"key": key, "note": "copy this now, it cannot be shown again"}


@router.delete("/dash/keys/{key_id}", status_code=204)
def revoke(key_id: int, request: Request):
    if not db.revoke_key(key_id, current_account(request)):
        raise HTTPException(404, "key not found")
