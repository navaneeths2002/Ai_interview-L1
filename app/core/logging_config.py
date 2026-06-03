"""
Structured JSON logging configuration.

Every log record is emitted as a single JSON line — easy to ingest by any
log aggregator (ELK, Loki, CloudWatch, etc.) or just grep in production.

Usage:
    from app.core.logging_config import get_logger
    logger = get_logger(__name__)
    logger.info("interview started", extra={"interview_id": "...", "tenant_id": "..."})
"""

import logging
import sys
from pythonjsonlogger.json import JsonFormatter

from app.core.config import settings

# ── Custom formatter ───────────────────────────────────────────────────────────

class InterviewJsonFormatter(JsonFormatter):
    """Extends pythonjsonlogger with a fixed set of always-present fields."""

    def add_fields(self, log_record: dict, record: logging.LogRecord, message_dict: dict) -> None:
        super().add_fields(log_record, record, message_dict)

        # Rename levelname → level for brevity
        log_record["level"]   = log_record.pop("levelname", record.levelname)
        log_record["logger"]  = record.name
        log_record["env"]     = settings.app_env

        # Remove the redundant 'name' key pythonjsonlogger adds
        log_record.pop("name", None)


# ── Root setup ─────────────────────────────────────────────────────────────────

def setup_logging() -> None:
    """
    Call once at application startup (inside FastAPI lifespan).

    - JSON output on stdout in production
    - Human-readable output in development (still via logging so level/format are consistent)
    """
    level_str = "DEBUG" if settings.app_env == "development" else "INFO"
    level = getattr(logging, level_str, logging.INFO)

    handler = logging.StreamHandler(sys.stdout)

    if settings.app_env == "development":
        # Readable format for local dev
        fmt = "%(asctime)s  %(levelname)-8s  %(name)s  %(message)s"
        handler.setFormatter(logging.Formatter(fmt, datefmt="%H:%M:%S"))
    else:
        # JSON for production
        handler.setFormatter(
            InterviewJsonFormatter(
                fmt="%(asctime)s %(levelname)s %(name)s %(message)s",
                datefmt="%Y-%m-%dT%H:%M:%SZ",
            )
        )

    root = logging.getLogger()
    root.setLevel(level)

    # Remove any existing handlers (uvicorn adds its own on import)
    root.handlers.clear()
    root.addHandler(handler)

    # Quieten noisy third-party libraries
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("livekit").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.INFO)


def get_logger(name: str) -> logging.Logger:
    """Return a named logger.  Call at module level: logger = get_logger(__name__)"""
    return logging.getLogger(name)
