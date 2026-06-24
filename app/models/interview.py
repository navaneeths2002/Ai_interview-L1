from datetime import datetime

from sqlalchemy import String, Integer, Boolean, Text, DateTime, Float
from sqlalchemy.dialects.postgresql import JSONB, ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import BaseModel


class Interview(BaseModel):
    """Main interview record — created when ATS triggers L1."""
    __tablename__ = "interviews"

    candidate_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    job_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)

    # scheduled, in_progress, completed, failed, no_show
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="scheduled")

    # browser (Phase 1), phone (Phase 2)
    mode: Mapped[str] = mapped_column(String(20), nullable=False, default="browser")

    join_url: Mapped[str] = mapped_column(String(500), nullable=True)
    join_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)

    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_seconds: Mapped[int] = mapped_column(Integer, nullable=True)

    recording_s3_key: Mapped[str] = mapped_column(String(500), nullable=True)
    transcript_s3_key: Mapped[str] = mapped_column(String(500), nullable=True)


class InterviewContext(BaseModel):
    """AI-generated interview strategy built from resume + ATS score + JD."""
    __tablename__ = "interview_contexts"

    # unique=True prevents duplicate context rows when context_builder is retried
    interview_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True, index=True)

    # What to verify from resume claims
    skills_to_validate: Mapped[list] = mapped_column(ARRAY(Text), nullable=True)

    # Gaps from ATS score — AI will ask about these
    gaps_to_probe: Mapped[list] = mapped_column(ARRAY(Text), nullable=True)

    # Projects on resume — AI will ask candidate to explain
    projects_to_ask: Mapped[list] = mapped_column(ARRAY(Text), nullable=True)

    # Specific experience claims to verify
    experience_to_verify: Mapped[dict] = mapped_column(JSONB, nullable=True)

    # LLM-generated plain text strategy summary
    interview_strategy: Mapped[str] = mapped_column(Text, nullable=True)

    # Full question flow for this interview (loaded into LangGraph)
    question_flow: Mapped[dict] = mapped_column(JSONB, nullable=True)

    # Role-tuned evaluation weights — LLM-determined at trigger, consumed by
    # evaluation_engine. Shape:
    # {
    #   "role_category": "client_facing",
    #   "weights": {"jd_fit":25,"communication":40,"behavioral":20,"confidence":15,"ats":10},
    #   "rationale": "<one line why>",
    #   "source": "llm" | "default",
    #   "generated_at": "<ISO-8601>"
    # }
    # Null for interviews created before this feature → evaluation falls back to defaults.
    evaluation_weights: Mapped[dict] = mapped_column(JSONB, nullable=True)


class InterviewTranscript(BaseModel):
    """
    One row per interview — ordered JSONB array of all conversation turns.

    Each element in `turns`:
      {
        "speaker":   "ai" | "candidate",
        "message":   "<text>",
        "spoken_at": "<ISO-8601 UTC timestamp>",
        "node":      "<LangGraph stage name | null>"
      }

    Turns are appended atomically via PostgreSQL ON CONFLICT upsert so the
    array order always matches the chronological order of the conversation —
    no sorting needed when reading.
    """
    __tablename__ = "interview_transcripts"

    # UNIQUE: exactly one row per interview — no cross-interview mixing
    interview_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True, index=True)

    # Ordered list of every turn, appended via || upsert
    turns: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)

    # Denormalised count — used by recovery queries instead of subquery EXISTS
    turn_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class InterviewExtractedData(BaseModel):
    """Structured data extracted from the conversation by the evaluation engine."""
    __tablename__ = "interview_extracted_data"

    interview_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True, index=True)

    # JSONB document — replaces the 12 individual typed columns.
    # Shape (mirrors the "extracted" key returned by Claude in evaluation_engine.py):
    # {
    #   "current_company":        str   | null,
    #   "current_role":           str   | null,
    #   "total_experience_years": float | null,
    #   "current_ctc":            int   | null,   # annual INR
    #   "expected_ctc":           int   | null,   # annual INR
    #   "notice_period_days":     int   | null,
    #   "notice_negotiable":      bool  | null,
    #   "relocation_willing":     bool  | null,
    #   "preferred_locations":    [str, ...],
    #   "work_authorization":     str   | null,
    #   "earliest_joining":       str   | null,
    # }
    extracted: Mapped[dict] = mapped_column(JSONB, nullable=True)

    # Full raw Claude output saved for audit / re-processing
    raw_extraction: Mapped[dict] = mapped_column(JSONB, nullable=True)


class InterviewScore(BaseModel):
    """Final evaluation scores generated by Claude Sonnet after the interview."""
    __tablename__ = "interview_scores"

    interview_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True, index=True)

    communication_score: Mapped[int] = mapped_column(Integer, nullable=True)  # 1-10
    confidence_score: Mapped[int] = mapped_column(Integer, nullable=True)     # 1-10
    jd_fit_score: Mapped[int] = mapped_column(Integer, nullable=True)         # 1-10
    behavioral_score: Mapped[int] = mapped_column(Integer, nullable=True)     # 1-10

    salary_fit: Mapped[bool] = mapped_column(Boolean, nullable=True)
    experience_validated: Mapped[bool] = mapped_column(Boolean, nullable=True)

    overall_score: Mapped[int] = mapped_column(Integer, nullable=True)        # 0-100

    # proceed_to_l2, reject, hold
    recommendation: Mapped[str] = mapped_column(String(20), nullable=True)

    # LLM reasoning — shown to recruiter as summary
    ai_reasoning: Mapped[str] = mapped_column(Text, nullable=True)

    # Recruiter can override the AI recommendation
    recruiter_override: Mapped[str] = mapped_column(String(20), nullable=True)
    override_reason: Mapped[str] = mapped_column(Text, nullable=True)
    override_by: Mapped[str] = mapped_column(String(36), nullable=True)
    override_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)


class InterviewCost(BaseModel):
    """
    Real (measured) marginal cost of one interview — written incrementally by
    three sources and finalized at interview end:

      • trigger      → strategy Claude tokens
      • interview    → conversation LLM tokens, TTS chars, STT seconds, duration, avatar
      • evaluation   → evaluation Claude tokens

    `usage` is a merged JSONB document (see app/services/pricing.compute_cost for
    the keys). `cost` is the per-tool breakdown; `total_usd` is the sum.
    """
    __tablename__ = "interview_costs"

    interview_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True, index=True)

    usage: Mapped[dict] = mapped_column(JSONB, nullable=True)   # merged raw usage
    cost:  Mapped[dict] = mapped_column(JSONB, nullable=True)   # per-tool USD breakdown
    total_usd: Mapped[float] = mapped_column(Float, nullable=True)
