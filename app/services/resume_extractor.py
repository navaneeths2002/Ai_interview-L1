from datetime import datetime


def extract_resume_data(parsed_resume: dict) -> dict:
    """
    Parses the ATS resume parser response.
    The response is keyed by filename e.g. { "resume.pdf": { ... } }
    We extract the first (and usually only) value.
    """
    # Get the resume data — it's nested under the filename key
    resume_data = next(iter(parsed_resume.values()), {})

    current_company, current_role = _get_current_position(resume_data.get("experience", []))
    total_experience = _calculate_total_experience(resume_data.get("experience", []))
    language_names = _extract_language_names(resume_data.get("languages", []))

    return {
        "first_name": resume_data.get("first_name", ""),
        "last_name": resume_data.get("last_name", ""),
        "email": resume_data.get("email", ""),
        "phone": resume_data.get("phone", ""),
        "skills": resume_data.get("skills", []),
        "certifications": resume_data.get("certifications", []),
        "languages": language_names,
        "education": resume_data.get("education", []),
        "experience": resume_data.get("experience", []),
        "projects": resume_data.get("projects", []),
        "current_company": current_company,
        "current_role": current_role,
        "total_experience_years": total_experience,
    }


def _get_current_position(experience: list) -> tuple[str, str]:
    """
    Finds the most recent job.
    'till date' or current year = current position.
    """
    if not experience:
        return "", ""

    current_year = datetime.now().year

    for exp in experience:
        end = exp.get("end_year", "")
        if str(end).lower() in ("till date", "present", "current", str(current_year)):
            return exp.get("company", ""), exp.get("position", "")

    # If no current job found, return the first one (most recent listed)
    return experience[0].get("company", ""), experience[0].get("position", "")


def _calculate_total_experience(experience: list) -> float:
    """
    Calculates total years of experience from the experience array.
    Uses start_year of earliest job to today.
    """
    if not experience:
        return 0.0

    current_year = datetime.now().year
    years = []

    for exp in experience:
        start = exp.get("start_year")
        if start and isinstance(start, int):
            years.append(start)

    if not years:
        return 0.0

    earliest_start = min(years)
    total = round(current_year - earliest_start, 1)
    return max(total, 0.0)


def _extract_language_names(languages: list) -> list[str]:
    """
    Languages come as [{ "name": "English", "proficiency_level": "" }]
    We just want ["English", "Malayalam", ...]
    """
    return [lang.get("name", "") for lang in languages if lang.get("name")]
