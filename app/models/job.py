from sqlalchemy import String, Integer, BigInteger, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import BaseModel


class Job(BaseModel):
    __tablename__ = "jobs"
    __table_args__ = (
        # Prevent duplicate job rows when ATS retries or replays a trigger.
        UniqueConstraint("tenant_id", "ats_job_id", name="uq_jobs_tenant_ats_id"),
    )

    # ID from the ATS system
    ats_job_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)

    position_title: Mapped[str] = mapped_column(String(255), nullable=False)
    department: Mapped[str] = mapped_column(String(255), nullable=True)
    location: Mapped[str] = mapped_column(String(255), nullable=True)
    position_type: Mapped[str] = mapped_column(String(50), nullable=True)  # full_time, contract
    min_experience_years: Mapped[int] = mapped_column(Integer, nullable=True)

    critical_skills: Mapped[list] = mapped_column(ARRAY(Text), nullable=True)
    optional_skills: Mapped[list] = mapped_column(ARRAY(Text), nullable=True)
    soft_skills: Mapped[list] = mapped_column(ARRAY(Text), nullable=True)

    salary_min: Mapped[int] = mapped_column(BigInteger, nullable=True)
    salary_max: Mapped[int] = mapped_column(BigInteger, nullable=True)

    jd_text: Mapped[str] = mapped_column(Text, nullable=True)
