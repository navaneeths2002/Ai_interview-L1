from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware


EXCLUDED_PATHS = ["/health", "/docs", "/openapi.json", "/redoc"]
EXCLUDED_PREFIXES = ["/interview/", "/static/"]

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
    return False


class TenantMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if _is_excluded(request.url.path):
            return await call_next(request)

        tenant_id = request.headers.get("X-Tenant-ID")
        if not tenant_id:
            return JSONResponse(
                status_code=400,
                content={"detail": "X-Tenant-ID header is required"},
            )

        request.state.tenant_id = tenant_id
        return await call_next(request)
