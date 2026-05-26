from fastapi import APIRouter
from datetime import datetime, timezone

router = APIRouter()


@router.get("/health")
async def health_check():
    return {
        "status": "ok",
        "service": "interview-agent",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
