"""
Rate limiting — powered by slowapi (Starlette-native, no Redis required).

Storage backend: in-memory (MemoryStorage) by default.
Switch to Redis for multi-process setups:
    from limits.storage import RedisStorage
    limiter = Limiter(key_func=..., storage_uri=settings.redis_url)

Key functions:
    _get_tenant_id   — reads X-Tenant-ID header; falls back to IP
    _get_remote_ip   — raw client IP from X-Forwarded-For or request.client

Limits defined here are imported by the route modules.
"""

from fastapi import Request
from slowapi import Limiter
from slowapi.util import get_remote_address


# ── Key functions ──────────────────────────────────────────────────────────────

def _get_tenant_id(request: Request) -> str:
    """
    Rate-limit key for tenant-authenticated endpoints.
    Uses X-Tenant-ID header so each tenant has its own bucket.
    Falls back to client IP if header is absent (shouldn't happen after middleware).
    """
    tenant = request.headers.get("X-Tenant-ID", "").strip()
    return tenant if tenant else get_remote_address(request)


def _get_remote_ip(request: Request) -> str:
    """
    Rate-limit key for public/candidate endpoints (no tenant header).
    Reads X-Forwarded-For first (reverse proxy), falls back to direct connection.
    """
    forwarded_for = request.headers.get("X-Forwarded-For", "")
    if forwarded_for:
        # First IP in the chain is the real client
        return forwarded_for.split(",")[0].strip()
    return get_remote_address(request)


# ── Limiter instance ───────────────────────────────────────────────────────────

# Default key: tenant_id for tenant routes, IP for public routes.
# Individual routes can override with key_func=_get_remote_ip.
limiter = Limiter(key_func=_get_tenant_id, default_limits=[])


# ── Rate limit strings ─────────────────────────────────────────────────────────
# Import these in route modules as:
#   from app.core.rate_limiter import limiter, LIMIT_TRIGGER, ...

# Trigger a new interview — expensive, Claude + LiveKit involved
LIMIT_TRIGGER   = "5/minute"

# Evaluation + report — re-run is cheap but should not be hammered
LIMIT_EVALUATE  = "10/minute"
LIMIT_REPORT    = "10/minute"

# Token endpoint — candidate fetches this; limit per IP to prevent scraping
LIMIT_TOKEN     = "30/minute"

# Manual recovery endpoint — admin only, very low limit
LIMIT_RECOVERY  = "3/minute"
