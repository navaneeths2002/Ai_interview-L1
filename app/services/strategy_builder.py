import time
import json
import re
from datetime import datetime, timezone

import anthropic
from app.core.config import settings


# ── Role-tuned evaluation weights ────────────────────────────────────────────────
# The 4 interview dimensions share 90%; the ATS pre-score is a FIXED 10% prior
# (the candidate already cleared ATS to be shortlisted — see knowledge.md).
WEIGHT_KEYS         = ["jd_fit", "communication", "behavioral", "confidence"]
DEFAULT_DIM_WEIGHTS = {"jd_fit": 35, "communication": 25, "behavioral": 15, "confidence": 15}  # sum 90
ATS_WEIGHT          = 10
DIM_TOTAL           = 90        # the 4 dims must sum to exactly this
MIN_W, MAX_W        = 5, 60     # per-dimension clamp band (% of the full 100)


def _normalize_weights(raw: dict) -> tuple[dict, bool]:
    """
    Turn the LLM's proposed 4 interview-dimension weights into a safe, exact
    rubric. Clamps each to [MIN_W, MAX_W], normalizes the 4 to sum exactly 90,
    and appends the fixed ATS 10%. Returns (weights_with_ats, used_llm).
    Any malformed / missing / non-positive input → defaults (used_llm=False).
    """
    try:
        vals = {}
        for k in WEIGHT_KEYS:
            v = float(raw.get(k))
            if v <= 0:
                raise ValueError("non-positive weight")
            vals[k] = v
        total = sum(vals.values())
        if total <= 0:
            raise ValueError("zero total")
        # 1) Scale so the 4 dims sum to DIM_TOTAL, 2) clamp each to the band,
        #    3) integer-round, 4) distribute any drift back to dims that still
        #    have headroom — so the final result ALWAYS respects [MIN_W, MAX_W]
        #    AND sums to exactly DIM_TOTAL.
        scaled  = {k: vals[k] / total * DIM_TOTAL for k in WEIGHT_KEYS}
        clamped = {k: max(MIN_W, min(MAX_W, scaled[k])) for k in WEIGHT_KEYS}
        ints    = {k: int(round(clamped[k])) for k in WEIGHT_KEYS}

        drift = DIM_TOTAL - sum(ints.values())
        # Nudge ±1 at a time toward dims with room (feasible: 4 dims, band 5–60,
        # target 90 is always reachable). Bounded loop as a safety backstop.
        step  = 1 if drift > 0 else -1
        order = sorted(WEIGHT_KEYS, key=lambda k: ints[k], reverse=(drift > 0))
        guard = 0
        while drift != 0 and guard < 1000:
            for k in order:
                if drift == 0:
                    break
                if MIN_W <= ints[k] + step <= MAX_W:
                    ints[k] += step
                    drift  -= step
            guard += 1

        ints["ats"] = ATS_WEIGHT
        return ints, True
    except (TypeError, ValueError, KeyError):
        d = dict(DEFAULT_DIM_WEIGHTS)
        d["ats"] = ATS_WEIGHT
        return d, False


def _build_weights_doc(strategy_raw: dict) -> dict:
    """Assemble the evaluation_weights document stored on InterviewContext."""
    weights, used_llm = _normalize_weights(strategy_raw.get("evaluation_weights") or {})
    rationale = strategy_raw.get("weights_rationale") or "Standard L1 screening weighting applied."
    role_cat  = strategy_raw.get("role_category") or "general"
    return {
        "role_category": str(role_cat)[:50],
        "weights":       weights,                       # 4 dims (sum 90) + ats=10 → total 100
        "rationale":     str(rationale)[:500],
        "source":        "llm" if used_llm else "default",
        "generated_at":  datetime.now(timezone.utc).isoformat(),
    }


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

Based on this profile, generate a focused interview strategy AND role-tuned
evaluation weights in JSON format:
{{
  "skills_to_validate": ["skill1", "skill2"],
  "gaps_to_probe": ["gap1", "gap2"],
  "experience_to_verify": ["claim1", "claim2"],
  "projects_to_ask": ["project1"],
  "strategy_summary": "2-3 sentence summary of how to approach this interview",
  "role_category": "<one of: technical_ic | client_facing | leadership | support_ops | general>",
  "evaluation_weights": {{
    "jd_fit": <int>, "communication": <int>, "behavioral": <int>, "confidence": <int>
  }},
  "weights_rationale": "<ONE sentence: why these weights suit THIS specific role>"
}}

Weighting guidance — set evaluation_weights based on what matters MOST for this role:
- These 4 numbers are PERCENTAGES that should sum to ~90 (the ATS pre-screen takes
  the remaining fixed 10%, so do NOT include ATS here).
- jd_fit        = alignment with the job's required skills / domain
- communication = clarity, fluency, articulation
- behavioral    = professionalism, stability, attitude
- confidence    = certainty, directness
- Example: a client-facing/sales role → communication high; a deep technical IC role
  → jd_fit high; a leadership role → behavioral high. Each value must be between 5 and 60.

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

    # Attach the normalized, guardrailed evaluation-weights document (always valid).
    strategy["evaluation_weights_doc"] = _build_weights_doc(strategy)

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
    strategy = {
        "skills_to_validate": [],
        "gaps_to_probe": missing_skills,
        "experience_to_verify": [],
        "projects_to_ask": [],
        "strategy_summary": "Conduct standard HR screening covering experience, salary, notice period, and skill gaps.",
    }
    # Default (non-LLM) weights document — keeps the contract consistent.
    strategy["evaluation_weights_doc"] = _build_weights_doc(strategy)
    return strategy
