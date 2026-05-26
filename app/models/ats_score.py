from sqlalchemy import String, Integer, Text
from sqlalchemy.dialects.postgresql import JSONB, ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import BaseModel


class AtsScore(BaseModel):
    __tablename__ = "ats_scores"

    candidate_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    job_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)

    total_score: Mapped[int] = mapped_column(Integer, nullable=True)
    critical_skills_score: Mapped[int] = mapped_column(Integer, nullable=True)
    experience_score: Mapped[int] = mapped_column(Integer, nullable=True)
    education_score: Mapped[int] = mapped_column(Integer, nullable=True)
    soft_skills_score: Mapped[int] = mapped_column(Integer, nullable=True)
    certifications_score: Mapped[int] = mapped_column(Integer, nullable=True)

    # Skills in JD but missing from resume — used to generate gap questions
    missing_skills: Mapped[list] = mapped_column(ARRAY(Text), nullable=True)
    strong_areas: Mapped[list] = mapped_column(ARRAY(Text), nullable=True)
    risk_flags: Mapped[list] = mapped_column(ARRAY(Text), nullable=True)

    # Full raw breakdown from ATS
    score_breakdown: Mapped[dict] = mapped_column(JSONB, nullable=True)
