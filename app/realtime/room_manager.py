from livekit.api import LiveKitAPI, AccessToken, VideoGrants, CreateRoomRequest
from app.core.config import settings


def _http_url() -> str:
    return settings.livekit_url.replace("wss://", "https://").replace("ws://", "http://")


async def create_interview_room(interview_id: str) -> str:
    """Creates a LiveKit room. Returns the room name."""
    room_name = f"interview-{interview_id}"
    async with LiveKitAPI(
        url=_http_url(),
        api_key=settings.livekit_api_key,
        api_secret=settings.livekit_api_secret,
    ) as lk:
        await lk.room.create_room(
            CreateRoomRequest(
                name=room_name,
                empty_timeout=30,    # close room 30s after last participant leaves
                max_participants=3,  # agent + candidate + simli-avatar
            )
        )
    return room_name


def generate_candidate_token(room_name: str, candidate_name: str, interview_id: str) -> str:
    """Generates a JWT token for the candidate to join the LiveKit room."""
    token = (
        AccessToken(
            api_key=settings.livekit_api_key,
            api_secret=settings.livekit_api_secret,
        )
        .with_identity(f"candidate-{interview_id}")
        .with_name(candidate_name)
        .with_grants(
            VideoGrants(
                room_join=True,
                room=room_name,
                can_publish=True,
                can_subscribe=True,
            )
        )
    )
    return token.to_jwt()
