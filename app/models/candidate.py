from sqlalchemy import String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import BaseModel


class Candidate(BaseModel):
    __tablename__ = "candidates"
    __table_args__ = (
        # Prevent duplicate candidates when ATS retries or replays a trigger.
        # Scoped per tenant so two tenants can have the same ATS candidate ID.
        UniqueConstraint("tenant_id", "ats_candidate_id", name="uq_candidates_tenant_ats_id"),
    )

    # ID from the ATS system (not our UUID)
    ats_candidate_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)

    first_name: Mapped[str] = mapped_column(String(100), nullable=False)
    last_name: Mapped[str] = mapped_column(String(100), nullable=True)
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    phone: Mapped[str] = mapped_column(String(20), nullable=True)

    # JSONB profile document — replaces the CandidateProfile table.
    # Shape:
    # {
    #   "total_experience_years": float | null,
    #   "current_company":        str   | null,
    #   "current_role":           str   | null,
    #   "skills":                 [str, ...],
    #   "certifications":         [str, ...],
    #   "languages":              [str, ...],
    #   "education":              [...],
    #   "experience":             [...],
    #   "projects":               [...],
    #   "resume_s3_key":          str   | null,
    #   "parsed_s3_key":          str   | null,
    # }
    profile: Mapped[dict] = mapped_column(JSONB, nullable=True)


# ---------------------------------------------------------------------------
# DEPRECATED — kept only so existing Alembic migrations don't break.
# New code writes candidate data into Candidate.profile (JSONB above).
# This class will be removed once all migrations referencing candidate_profiles
# have been squashed.
# ---------------------------------------------------------------------------
class CandidateProfile(BaseModel):
    __tablename__ = "candidate_profiles"

    candidate_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
