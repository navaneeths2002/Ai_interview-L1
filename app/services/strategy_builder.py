import time
import json
import re
import anthropic
from app.core.config import settings


def build_interview_strategy(
    candidate_name: str,
    current_role: str,
    current_company: str,
    total_experience_years: float,
    skills: list[str],
    experience: list[dict],
    projects: list[dict],
    missing_skills: list[str],
    strong_areas: list[str],
    risk_flags: list[str],
    job_title: str,
    critical_skills: list[str],
    ats_total_score: float,
) -> dict:
    """
    Calls Claude to generate a personalized interview strategy.
    Returns structured guidance for LangGraph to use during the interview.
    """
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    prompt = f"""
You are an expert HR interviewer preparing for an L1 screening call.

CANDIDATE PROFILE:
- Name: {candidate_name}
- Current Role: {current_role} at {current_company}
- Total Experience: {total_experience_years} years
- Skills: {', '.join(skills)}
- ATS Score: {ats_total_score}/100

JOB BEING APPLIED FOR:
- Position: {job_title}
- Required Skills: {', '.join(critical_skills)}
- Missing Skills (in JD but not in resume): {', '.join(missing_skills) if missing_skills else 'None'}
- Strong Areas: {', '.join(strong_areas) if strong_areas else 'None'}
- Risk Flags: {', '.join(risk_flags) if risk_flags else 'None'}

RECENT EXPERIENCE:
{_format_experience(experience)}

Based on this profile, generate a focused interview strategy in JSON format:
{{
  "skills_to_validate": ["skill1", "skill2"],
  "gaps_to_probe": ["gap1", "gap2"],
  "experience_to_verify": ["claim1", "claim2"],
  "projects_to_ask": ["project1"],
  "strategy_summary": "2-3 sentence summary of how to approach this interview"
}}

Return ONLY valid JSON. No explanation outside the JSON.
"""

    raw = _call_with_retry(client, prompt)
    if raw is None:
        return _default_strategy(missing_skills)

    try:
        strategy = json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r'\{.*\}', raw, re.DOTALL)
        strategy = json.loads(match.group()) if match else _default_strategy(missing_skills)

    return strategy


def _call_with_retry(client: anthropic.Anthropic, prompt: str, retries: int = 3) -> str | None:
    """Calls Claude with exponential backoff on 529/529 overload errors."""
    for attempt in range(retries):
        try:
            message = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=1024,
                messages=[{"role": "user", "content": prompt}],
            )
            return message.content[0].text.strip()
        except anthropic.APIStatusError as e:
            if e.status_code in (429, 529) and attempt < retries - 1:
                wait = 2 ** attempt  # 1s, 2s, 4s
                time.sleep(wait)
                continue
            return None
        except Exception:
            return None


def _format_experience(experience: list[dict]) -> str:
    if not experience:
        return "No experience listed"
    lines = []
    for exp in experience[:3]:  # top 3 only
        lines.append(f"- {exp.get('position')} at {exp.get('company')} ({exp.get('start_year')} - {exp.get('end_year')})")
    return "\n".join(lines)


def _default_strategy(missing_skills: list[str]) -> dict:
    return {
        "skills_to_validate": [],
        "gaps_to_probe": missing_skills,
        "experience_to_verify": [],
        "projects_to_ask": [],
        "strategy_summary": "Conduct standard HR screening covering experience, salary, notice period, and skill gaps.",
    }
