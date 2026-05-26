def extract_ats_data(ats_score_data: dict, critical_skills: list[str], candidate_skills: list[str], resume_filename: str = "") -> dict:
    """
    Parses ATS score response and calculates missing skills.

    ATS score response structure:
    {
      "results": [
        {
          "file": "Rajiv_Chaudhary.pdf.json",
          "result": {
            "total_score": 35.25,
            "by_parameter": { ... }
          }
        }
      ]
    }
    Multiple resumes can be in results — we match by filename.
    """
    results = ats_score_data.get("results", [])
    if not results:
        return _empty_score()

    # Match by filename — e.g. "Rajiv_Chaudhary.pdf" matches "Rajiv_Chaudhary.pdf.json"
    matched = None
    if resume_filename:
        for r in results:
            file_key = r.get("file", "")
            if resume_filename.replace(".pdf", "") in file_key:
                matched = r
                break

    # Fallback to first result if no match found
    first_result = (matched or results[0]).get("result", {})
    by_parameter = first_result.get("by_parameter", {})
    total_score = first_result.get("total_score", 0.0)

    missing_skills = _calculate_missing_skills(critical_skills, candidate_skills)
    strong_areas = _calculate_strong_areas(by_parameter)
    risk_flags = _calculate_risk_flags(by_parameter, total_score)

    return {
        "total_score": total_score,
        "critical_skills_score": by_parameter.get("skills", {}).get("score", 0.0),
        "experience_score": by_parameter.get("experience", {}).get("score", 0.0),
        "education_score": by_parameter.get("education", {}).get("score", 0.0),
        "soft_skills_score": by_parameter.get("soft_skills", {}).get("score", 0.0),
        "certifications_score": by_parameter.get("certifications", {}).get("score", 0.0),
        "missing_skills": missing_skills,
        "strong_areas": strong_areas,
        "risk_flags": risk_flags,
        "score_breakdown": by_parameter,
    }


def _calculate_missing_skills(critical_skills: list[str], candidate_skills: list[str]) -> list[str]:
    """
    Finds skills required by JD but missing from candidate resume.
    Case-insensitive comparison.
    """
    candidate_skills_lower = {s.lower() for s in candidate_skills}
    missing = [
        skill for skill in critical_skills
        if skill.lower() not in candidate_skills_lower
    ]
    return missing


def _calculate_strong_areas(by_parameter: dict) -> list[str]:
    """Parameters where candidate passed."""
    return [
        param for param, data in by_parameter.items()
        if data.get("passed") is True
    ]


def _calculate_risk_flags(by_parameter: dict, total_score: float) -> list[str]:
    """Flags potential concerns."""
    flags = []
    if total_score < 30:
        flags.append("low_overall_score")
    if not by_parameter.get("experience", {}).get("passed"):
        flags.append("experience_mismatch")
    if not by_parameter.get("skills", {}).get("passed"):
        flags.append("critical_skills_missing")
    return flags


def _empty_score() -> dict:
    return {
        "total_score": 0.0,
        "critical_skills_score": 0.0,
        "experience_score": 0.0,
        "education_score": 0.0,
        "soft_skills_score": 0.0,
        "certifications_score": 0.0,
        "missing_skills": [],
        "strong_areas": [],
        "risk_flags": [],
        "score_breakdown": {},
    }
