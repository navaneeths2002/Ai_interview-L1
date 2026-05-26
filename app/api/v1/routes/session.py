from fastapi import APIRouter, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import Depends
from sqlalchemy import select

from app.db.session import get_db
from app.models.interview import Interview
from app.models.candidate import Candidate
from app.realtime.room_manager import generate_candidate_token

router = APIRouter()


@router.get("/interviews/{interview_id}/token")
async def get_candidate_token(
    interview_id: str,
    db: AsyncSession = Depends(get_db),
):
    """
    Candidate calls this when they open the join link.
    Returns a LiveKit token to enter the voice room.
    No X-Tenant-ID required — candidate doesn't know their tenant.
    """
    result = await db.execute(
        select(Interview).where(Interview.id == interview_id)
    )
    interview = result.scalar_one_or_none()

    if not interview:
        raise HTTPException(status_code=404, detail="Interview not found")

    if interview.status == "completed":
        raise HTTPException(status_code=400, detail="Interview already completed")

    # Get candidate name for the room
    candidate_result = await db.execute(
        select(Candidate).where(Candidate.id == interview.candidate_id)
    )
    candidate = candidate_result.scalar_one_or_none()
    candidate_name = f"{candidate.first_name} {candidate.last_name}" if candidate else "Candidate"

    room_name = f"interview-{interview_id}"
    token = generate_candidate_token(room_name, candidate_name, interview_id)

    return {
        "token": token,
        "room_name": room_name,
        "livekit_url": "wss://ai-interview-0zs0av8r.livekit.cloud",
        "candidate_name": candidate_name,
        "interview_id": interview_id,
    }
