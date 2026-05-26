"""
Phase 7 — Interview Report Model
"""
from datetime import datetime

from sqlalchemy import String, Text, Integer, DateTime
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import BaseModel


class InterviewReport(BaseModel):
    """Recruiter-ready report generated after evaluation completes."""
    __tablename__ = "interview_reports"

    interview_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)

    # Denormalised for fast access
    candidate_name:  Mapped[str] = mapped_column(String(255), nullable=True)
    position_title:  Mapped[str] = mapped_column(String(255), nullable=True)
    ats_score:       Mapped[int] = mapped_column(Integer, nullable=True)

    # Final verdict
    overall_score:   Mapped[int] = mapped_column(Integer, nullable=True)
    recommendation:  Mapped[str] = mapped_column(String(20), nullable=True)

    # Structured report payload (everything needed for any UI)
    report_data: Mapped[dict] = mapped_column(JSONB, nullable=True)

    # Direct URL to the HTML report (open in browser → Ctrl+P → Save as PDF)
    report_url: Mapped[str] = mapped_column(String(500), nullable=True)

    # Pre-rendered HTML (print-to-PDF ready)
    report_html: Mapped[str] = mapped_column(Text, nullable=True)

    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
