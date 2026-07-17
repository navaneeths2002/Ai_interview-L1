"""
Connector API-key gatekeeper
============================
Protects the ATS integration endpoint. The ATS sends the key WE issued
(CONNECTOR_API_KEY) as the `X-API-Key` header on every call. A missing or wrong
key is rejected with 401 before any work happens. Constant-time compare so the
key can't be guessed via response timing.

Usage:
    @router.post("/integration/interview",
                 dependencies=[Depends(verify_connector_api_key)])
"""

from __future__ import annotations

import secrets

from fastapi import Header, HTTPException

from app.core.config import settings


async def verify_connector_api_key(
    x_api_key: str = Header(None, alias="X-API-Key"),
) -> None:
    expected = settings.connector_api_key or ""
    # Fail closed: if no key is configured on the server, refuse rather than
    # silently allowing anyone through.
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="Integration API key not configured on the server",
        )
    if not x_api_key or not secrets.compare_digest(x_api_key, expected):
        raise HTTPException(status_code=401, detail="Invalid or missing API key")
