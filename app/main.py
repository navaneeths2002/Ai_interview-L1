from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
from pathlib import Path

from app.core.config import settings
from app.core.middleware import TenantMiddleware
from app.api.v1.routes import health, interviews, session, evaluation, reports, recovery
from app.services.recovery import run_all_recovery
from app.workers.scheduler import start_scheduler, stop_scheduler


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── Startup ────────────────────────────────────────────────────────────────
    # Run an immediate recovery pass so any interviews broken during the last
    # server downtime are fixed before we start accepting new traffic.
    await run_all_recovery()
    start_scheduler()

    yield

    # ── Shutdown ───────────────────────────────────────────────────────────────
    stop_scheduler()


app = FastAPI(
    title="Interview Agent",
    description="L1 AI HR Screening Microservice",
    version="1.0.0",
    docs_url="/docs" if settings.app_env == "development" else None,
    lifespan=lifespan,
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Tenant isolation — exclude interview page (candidate doesn't send tenant header)
app.add_middleware(TenantMiddleware)

# Static files
app.mount("/static", StaticFiles(directory="app/static"), name="static")

# Routes
app.include_router(health.router,      tags=["Health"])
app.include_router(interviews.router,  prefix="/api/v1", tags=["Interviews"])
app.include_router(session.router,     prefix="/api/v1", tags=["Session"])
app.include_router(evaluation.router,  prefix="/api/v1", tags=["Evaluation"])
app.include_router(reports.router,     prefix="/api/v1", tags=["Reports"])
app.include_router(recovery.router,    prefix="/api/v1", tags=["Recovery"])


# Candidate interview page — served at /interview/{interview_id}
@app.get("/interview/{interview_id}", response_class=HTMLResponse)
async def interview_page(interview_id: str):
    html_path = Path("app/static/interview.html")
    return HTMLResponse(content=html_path.read_text(encoding="utf-8"))
