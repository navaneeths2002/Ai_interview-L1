import re

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware


EXCLUDED_PATHS = ["/health", "/docs", "/openapi.json", "/redoc"]
EXCLUDED_PREFIXES = ["/interview/", "/static/"]

# Tenant IDs must be 3–64 chars, alphanumeric + hyphens/underscores/dots only.
# Prevents injection characters while staying compatible with formats like
# "tenant-001" or UUID strings.
# Full DB-backed validation will be added in Phase 12 (JWT auth).
_TENANT_ID_RE = re.compile(r'^[a-zA-Z0-9_\-\.]{3,64}$')


def _is_excluded(path: str) -> bool:
    if path in EXCLUDED_PATHS:
        return True
    if any(path.startswith(p) for p in EXCLUDED_PREFIXES):
        return True
    # Candidate token endpoint — no tenant header required
    if path.startswith("/api/v1/interviews/") and path.endswith("/token"):
        return True
    # HTML report — opened directly in browser, no tenant header possible
    if path.startswith("/api/v1/interviews/") and path.endswith("/report/html"):
        return True
    # ATS integration endpoint — authenticated by X-API-Key (Connector key),
    # tenant is supplied in the request body, not the header.
    if path.startswith("/api/v1/integration/"):
        return True
    return False


class TenantMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if _is_excluded(request.url.path):
            return await call_next(request)

        tenant_id = request.headers.get("X-Tenant-ID", "").strip()
        if not tenant_id:
            return JSONResponse(
                status_code=400,
                content={"detail": "X-Tenant-ID header is required"},
            )

        # Validate format — blocks injection characters and obviously invalid values
        if not _TENANT_ID_RE.match(tenant_id):
            return JSONResponse(
                status_code=400,
                content={
                    "detail": (
                        "X-Tenant-ID must be 3–64 characters "
                        "(letters, digits, hyphens, underscores, dots only)"
                    )
                },
            )

        request.state.tenant_id = tenant_id
        return await call_next(request)
