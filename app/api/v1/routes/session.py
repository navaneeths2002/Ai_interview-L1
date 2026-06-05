from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import Depends
from sqlalchemy import select

from app.core.config import settings
from app.core.rate_limiter import limiter, LIMIT_TOKEN, _get_remote_ip
from app.core.security import verify_invite_token
from app.db.session import get_db
from app.models.interview import Interview
from app.models.candidate import Candidate
from app.realtime.room_manager import generate_candidate_token

router = APIRouter()

# Statuses that mean the interview is over — no token should be issued,
# but we return 200 with status so the browser can show a clean message.
_TERMINAL_STATUSES = {"completed", "expired", "failed"}


@router.get("/interviews/{interview_id}/token")
@limiter.limit(LIMIT_TOKEN, key_func=_get_remote_ip)
async def get_candidate_token(
    request: Request,
    interview_id: str,
    token: Optional[str] = Query(default=None),
    db: AsyncSession = Depends(get_db),
):
    """
    Candidate calls this when they open the join link.
    Returns a LiveKit token to enter the voice room.
    No X-Tenant-ID required — candidate doesn't know their tenant.

    Requires a signed invite token passed as ?token= query param.
    The token is generated when the interview is triggered and embedded
    in the join link that is emailed to the candidate.

    For terminal interviews (completed / expired / failed) returns 200 with
    token=null and status set — the browser shows a clean "interview over" screen
    instead of a broken page or a raw JS alert.
    """
    # Validate signed invite token before doing anything else
    if not token:
        raise HTTPException(
            status_code=401,
            detail="Missing invite token. Please use the link sent to your email.",
        )
    try:
        verify_invite_token(token, interview_id)
    except ValueError as e:
        raise HTTPException(status_code=401, detail=str(e))

    result = await db.execute(
        select(Interview).where(Interview.id == interview_id)
    )
    interview = result.scalar_one_or_none()

    if not interview:
        raise HTTPException(status_code=404, detail="Interview not found")

    # Get candidate name for display regardless of status
    candidate_result = await db.execute(
        select(Candidate).where(Candidate.id == interview.candidate_id)
    )
    candidate = candidate_result.scalar_one_or_none()
    candidate_name = (
        f"{candidate.first_name} {candidate.last_name or ''}".strip()
        if candidate else "Candidate"
    )

    # Expired join link — check before status so a scheduled-but-expired link
    # is caught even if the DB status hasn't been updated yet.
    if (
        interview.join_expires_at
        and datetime.now(timezone.utc) > interview.join_expires_at
        and interview.status not in _TERMINAL_STATUSES
    ):
        return {
            "status":         "expired",
            "token":          None,
            "room_name":      None,
            "livekit_url":    None,
            "candidate_name": candidate_name,
            "interview_id":   interview_id,
        }

    # Terminal interview — return 200 with null token so the browser can show
    # a proper "interview is over" screen rather than crashing or alerting.
    if interview.status in _TERMINAL_STATUSES:
        return {
            "status":         interview.status,
            "token":          None,
            "room_name":      None,
            "livekit_url":    None,
            "candidate_name": candidate_name,
            "interview_id":   interview_id,
        }

    room_name = f"interview-{interview_id}"
    token = generate_candidate_token(room_name, candidate_name, interview_id)

    return {
        "status":         interview.status,        # "scheduled" or "in_progress"
        "token":          token,
        "room_name":      room_name,
        "livekit_url":    settings.livekit_url,    # reads from .env — no hardcoding
        "candidate_name": candidate_name,
        "interview_id":   interview_id,
    }
