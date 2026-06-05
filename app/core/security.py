"""
Invite token utilities — generate and validate signed candidate join tokens.
Uses python-jose (already in requirements.txt).
"""
import logging
from datetime import datetime, timedelta, timezone

from jose import JWTError, jwt

from app.core.config import settings

logger = logging.getLogger(__name__)

ALGORITHM = "HS256"


def create_invite_token(interview_id: str, candidate_email: str) -> str:
    """
    Generate a signed JWT invite token for a candidate.
    Embeds interview_id + candidate_email + expiry.
    Token is signed with SECRET_KEY — tamper-proof.
    """
    expire = datetime.now(timezone.utc) + timedelta(
        hours=settings.invite_token_expire_hours
    )
    payload = {
        "type":            "invite",
        "interview_id":    interview_id,
        "candidate_email": candidate_email,
        "exp":             expire,
    }
    token = jwt.encode(payload, settings.secret_key, algorithm=ALGORITHM)
    logger.info(
        f"[security] Invite token created for interview {interview_id} "
        f"(expires in {settings.invite_token_expire_hours}h)"
    )
    return token


def verify_invite_token(token: str, interview_id: str) -> dict:
    """
    Validate a candidate invite token.

    Checks:
      - Valid JWT signature (signed by us)
      - Not expired
      - token type == "invite"
      - interview_id in token matches the path param

    Returns the decoded payload on success.
    Raises ValueError with a human-readable message on any failure.
    """
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[ALGORITHM])
    except JWTError as e:
        raise ValueError(f"Invalid or expired invite link: {e}")

    if payload.get("type") != "invite":
        raise ValueError("This link is not a valid interview invite.")

    if payload.get("interview_id") != interview_id:
        raise ValueError("This invite link does not match the interview.")

    return payload
