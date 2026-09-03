from fastapi import Header, HTTPException

from timesfm_serve import db


def require_key(x_api_key: str | None = Header(default=None)) -> str:
    """returns the tenant name for a valid key"""
    if not x_api_key:
        raise HTTPException(401, "missing api key")
    tenant = db.tenant_for_key(x_api_key)
    if tenant is None:
        raise HTTPException(401, "invalid api key")
    return tenant
