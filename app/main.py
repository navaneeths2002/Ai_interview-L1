from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.exceptions import RequestValidationError
from pathlib import Path
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from app.core.config import settings
from app.core.logging_config import setup_logging, get_logger
from app.core.middleware import TenantMiddleware
from app.core.rate_limiter import limiter
from app.api.v1.routes import health, interviews, session, evaluation, reports, recovery, integration
from app.services.recovery import run_all_recovery
from app.workers.scheduler import start_scheduler, stop_scheduler

# ── Logging must be set up before anything else logs ──────────────────────────
setup_logging()
logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── Startup ────────────────────────────────────────────────────────────────
    logger.info("Interview Agent starting up", extra={"env": settings.app_env})

    # Run an immediate recovery pass so any interviews broken during the last
    # server downtime are fixed before we start accepting new traffic.
    await run_all_recovery()
    start_scheduler()

    logger.info("Interview Agent ready")
    yield

    # ── Shutdown ───────────────────────────────────────────────────────────────
    logger.info("Interview Agent shutting down")
    stop_scheduler()


app = FastAPI(
    title="Interview Agent",
    description="L1 AI HR Screening Microservice",
    version="1.0.0",
    docs_url="/docs" if settings.app_env == "development" else None,
    lifespan=lifespan,
)

# ── Rate limiter ───────────────────────────────────────────────────────────────
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)

# ── CORS ───────────────────────────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Tenant isolation — exclude interview page (candidate doesn't send tenant header)
app.add_middleware(TenantMiddleware)

# ── Static files ───────────────────────────────────────────────────────────────
app.mount("/static", StaticFiles(directory="app/static"), name="static")

# ── Routes ─────────────────────────────────────────────────────────────────────
app.include_router(health.router,      tags=["Health"])
app.include_router(interviews.router,  prefix="/api/v1", tags=["Interviews"])
app.include_router(session.router,     prefix="/api/v1", tags=["Session"])
app.include_router(evaluation.router,  prefix="/api/v1", tags=["Evaluation"])
app.include_router(reports.router,     prefix="/api/v1", tags=["Reports"])
app.include_router(recovery.router,    prefix="/api/v1", tags=["Recovery"])
app.include_router(integration.router, prefix="/api/v1", tags=["Integration"])


# ── Exception handlers ─────────────────────────────────────────────────────────

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    logger.warning(
        "Request validation failed",
        extra={"path": str(request.url.path), "errors": exc.errors()},
    )
    return JSONResponse(status_code=422, content={"detail": exc.errors()})


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.exception(
        "Unhandled exception",
        extra={"path": str(request.url.path), "method": request.method},
    )
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"},
    )


# ── Candidate interview page — served at /interview/{interview_id} ─────────────
@app.get("/interview/{interview_id}", response_class=HTMLResponse)
async def interview_page(interview_id: str):
    html_path = Path("app/static/interview.html")
    return HTMLResponse(content=html_path.read_text(encoding="utf-8"))
