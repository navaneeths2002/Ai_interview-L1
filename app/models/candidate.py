from sqlalchemy import String, Numeric, Text
from sqlalchemy.dialects.postgresql import JSONB, ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import BaseModel


class Candidate(BaseModel):
    __tablename__ = "candidates"

    # ID from the ATS system (not our UUID)
    ats_candidate_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)

    first_name: Mapped[str] = mapped_column(String(100), nullable=False)
    last_name: Mapped[str] = mapped_column(String(100), nullable=True)
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    phone: Mapped[str] = mapped_column(String(20), nullable=True)


class CandidateProfile(BaseModel):
    __tablename__ = "candidate_profiles"

    candidate_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)

    total_experience_years: Mapped[float] = mapped_column(Numeric(4, 1), nullable=True)
    current_company: Mapped[str] = mapped_column(String(255), nullable=True)
    current_role: Mapped[str] = mapped_column(String(255), nullable=True)

    # Arrays — list of skills, certifications, languages
    skills: Mapped[list] = mapped_column(ARRAY(Text), nullable=True)
    certifications: Mapped[list] = mapped_column(ARRAY(Text), nullable=True)
    languages: Mapped[list] = mapped_column(ARRAY(Text), nullable=True)

    # JSON — structured data with nested fields
    education: Mapped[dict] = mapped_column(JSONB, nullable=True)
    experience: Mapped[dict] = mapped_column(JSONB, nullable=True)
    projects: Mapped[dict] = mapped_column(JSONB, nullable=True)

    # S3 locations
    resume_s3_key: Mapped[str] = mapped_column(String(500), nullable=True)
    parsed_s3_key: Mapped[str] = mapped_column(String(500), nullable=True)
