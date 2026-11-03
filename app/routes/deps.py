from __future__ import annotations

import secrets

from fastapi import Header, HTTPException, Request

from app.service import Service


def get_service(request: Request) -> Service:
    return request.app.state.service


def require_api_key(request: Request, x_api_key: str | None = Header(None, description="Required only if the server sets URJA_API_KEYS.")) -> None:
    keys = request.app.state.settings.api_keys
    if not keys:
        return
    if not x_api_key or not any(secrets.compare_digest(x_api_key, k) for k in keys):
        raise HTTPException(status_code=401, detail="Missing or invalid X-API-Key")


def split_multi(values: list[str] | None) -> list[str]:
    """Accept both ?status=a&status=b and ?status=a,b."""
    out: list[str] = []
    for v in values or []:
        out += [p.strip() for p in v.split(",") if p.strip()]
    return out
