from __future__ import annotations

import re
from collections.abc import Sequence
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from backend.schemas.domain import (
    JobEvaluation,
    JobPosting,
    ResumeAnalysis,
    ScoreComponent,
)

from .gateway import ModelGateway


def _payload(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return dict(value)
    return {
        key: getattr(value, key)
        for key in dir(value)
        if not key.startswith("_") and not callable(getattr(value, key, None))
    }


CRITERIA = {
    "title": (2, 5, "Название должности"),
    "tasks": (3, 30, "Задачи"),
    "industry": (4, 25, "Сфера"),
    "required_years": (2, 20, "Годы опыта"),
    "languages": (2, 10, "Языки"),
    "skills": (3, 10, "Навыки"),
}

# Primary-score gates. The fixed gates apply to every session; only tasks,
# industry and skills can be overridden by the user's influence controls.
DEFAULT_MINIMUM_SCORES = {
    "title": 0,
    "required_years": 1,
    "languages": 1,
    "tasks": 2,
    "industry": 2,
    "skills": 2,
}


def _round_half_up(value: Decimal) -> int:
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _assessment_rows(
    job: JobPosting, analysis: ResumeAnalysis, minimum_scores: dict[str, int] | None = None
) -> list[ScoreComponent]:
    rows = []
    for key, (raw_max, weight, title) in CRITERIA.items():
        assessment = getattr(analysis, key)
        if assessment.score > raw_max:
            raise ValueError(f"{key}.score must be between 0 and {raw_max}")
        weighted = Decimal(assessment.score) / Decimal(raw_max) * Decimal(weight)
        rows.append(ScoreComponent(
            key=key, title=title, points=_round_half_up(weighted), max_points=weight,
            raw_points=assessment.score, raw_max_points=raw_max,
            minimum_points=(minimum_scores or {}).get(key),
            minimum_failed=(key in (minimum_scores or {}) and assessment.score < minimum_scores[key]),
            explanation=assessment.explanation or f"Оценка по шкале: {assessment.score}/{raw_max}",
            evidence=list(assessment.evidence),
        ))
    return rows


def _significant_terms(value: str) -> set[str]:
    stop = {
        "вакансия",
        "резюме",
        "профиль",
        "требует",
        "предлагает",
        "связана",
        "позиция",
        "работа",
        "работать",
        "который",
        "которая",
        "этот",
        "данные",
        "есть",
        "для",
    }
    return {
        token.lower() if len(token) <= 4 else token.lower()[:5]
        for token in re.findall(r"[\w-]+", value.lower())
        if len(token) >= 3 and token.lower() not in stop
    }


def _quote_grounded(quote: str, source_text: str) -> bool:
    quote_terms = _significant_terms(quote)
    if not quote_terms:
        return False
    overlap = quote_terms & _significant_terms(source_text)
    required = 1 if len(quote_terms) <= 2 else max(2, (len(quote_terms) + 1) // 2)
    return len(overlap) >= required


_FOREIGN_LANGUAGE = (
    r"(?:английск\w*|english|немецк\w*|german|французск\w*|french|"
    r"испанск\w*|spanish|китайск\w*|chinese|итальянск\w*|italian|"
    r"португальск\w*|portuguese|арабск\w*|arabic|японск\w*|japanese|"
    r"корейск\w*|korean|турецк\w*|turkish|польск\w*|polish|чешск\w*|czech|"
    r"украинск\w*|ukrainian|белорусск\w*|belarusian|казахск\w*|kazakh|"
    r"узбекск\w*|uzbek|армянск\w*|armenian|грузинск\w*|georgian|иврит|hebrew|"
    r"нидерландск\w*|голландск\w*|dutch|шведск\w*|swedish|норвежск\w*|norwegian|"
    r"финск\w*|finnish|датск\w*|danish|иностранн\w*\s+язык\w*)"
)
_RUSSIAN_LANGUAGE = r"(?:русск\w*|russian)"
_LANGUAGE_LEVEL = (
    r"(?:a1|a2|b1|b2|c1|c2|fluent|advanced|intermediate|разговорн\w*)"
)
def _language_requirement_evidence(
    job: JobPosting, language_pattern: str
) -> list[str]:
    explicit_requirement = re.compile(
        rf"(?:знани\w*|владени\w*|требу\w*|обязател\w*|необходим\w*|уров(?:ень|ня))"
        rf"(?:\s+\w+){{0,4}}\s+{language_pattern}"
        rf"|{language_pattern}(?:\s+\w+){{0,4}}\s+{_LANGUAGE_LEVEL}"
        rf"|{_LANGUAGE_LEVEL}(?:\s+\w+){{0,3}}\s+{language_pattern}",
        re.I,
    )
    work_use = re.compile(
        rf"(?:переписк\w*|встреч\w*|переговор\w*|общени\w*|коммуникац\w*|"
        rf"документац\w*|презентац\w*|созвон\w*|работ\w*)"
        rf"(?:\s+\w+){{0,4}}\s+(?:на\s+|in\s+){language_pattern}",
        re.I,
    )
    benefit = re.compile(
        r"speaking\s+club|языков\w*\s+клуб|клуб\w*(?:\s+\w+){0,3}\s+английск\w*|"
        r"курс\w*(?:\s+\w+){0,3}\s+английск\w*|обучени\w*(?:\s+\w+){0,3}\s+английск\w*",
        re.I,
    )

    evidence: list[str] = []
    for required_skill in job.required_skills:
        value = str(required_skill)
        if re.search(language_pattern, value, re.I) and not benefit.search(value):
            evidence.append(value[:300])

    free_text = " ".join([job.title, job.description, *job.responsibilities])
    for clause in re.split(r"[.;\n]+", free_text):
        if not re.search(language_pattern, clause, re.I):
            continue
        explicit = bool(explicit_requirement.search(clause) or work_use.search(clause))
        if benefit.search(clause) and not explicit:
            continue
        if explicit:
            evidence.append(clause.strip()[:300])
    return list(dict.fromkeys(item for item in evidence if item))


def _ground_resume_analysis(
    analysis: ResumeAnalysis,
    job: JobPosting,
    profile: Any,
    resumes: Sequence[Any],
) -> ResumeAnalysis:
    structured_job_fields = [
        str(getattr(job, field))
        for field in (
            "payment_frequency", "required_experience", "employment_type",
            "hiring_format", "work_schedule", "working_hours", "work_format",
        )
        if getattr(job, field, None)
    ]
    job_source = " ".join(
        [job.title, job.description, *job.responsibilities, *job.required_skills, *structured_job_fields]
    )
    resume_source = " ".join(
        [str(_payload(profile)), *[str(_payload(item)) for item in resumes]]
    )
    combined_source = f"{job_source} {resume_source}"
    for field_name in (
        "title",
        "tasks",
        "industry",
        "required_years",
        "languages",
        "skills",
    ):
        assessment = getattr(analysis, field_name)
        grounded = bool(assessment.evidence)
        for quote in assessment.evidence:
            normalized = str(quote).strip()
            if re.match(r"^вакансия\s*:", normalized, re.I):
                source = job_source
            elif re.match(r"^(?:резюме|ревюме|профиль)\s*:", normalized, re.I):
                source = resume_source
            else:
                source = combined_source
            if not _quote_grounded(normalized, source):
                grounded = False
                break
        if assessment.score > 0 and not grounded:
            assessment.score = 0
            assessment.confidence = 0
            assessment.explanation = (
                "Совпадение обнулено: evidence не подтверждено входными данными."
            )
            assessment.evidence = []
    foreign_requirements = _language_requirement_evidence(job, _FOREIGN_LANGUAGE)
    if not foreign_requirements:
        russian_requirements = _language_requirement_evidence(job, _RUSSIAN_LANGUAGE)
        analysis.languages.score = 2
        analysis.languages.confidence = 1
        analysis.languages.evidence = russian_requirements
        analysis.languages.explanation = (
            "Иностранный язык не требуется; явно требуется только русский язык."
            if russian_requirements
            else "В вакансии нет явного требования иностранного языка; критерий засчитан полностью."
        )
    if analysis.category and not _quote_grounded(analysis.category, job_source):
        analysis.category = ""
    return analysis


async def evaluate(
    job: JobPosting,
    profile: Any,
    resumes: Sequence[Any],
    gateway: ModelGateway,
    minimum_scores: dict[str, int] | None = None,
) -> JobEvaluation:
    """Evaluate a vacancy only against selected resumes."""
    effective_minimums = dict(DEFAULT_MINIMUM_SCORES)
    supplied = minimum_scores or {}
    unknown = set(supplied) - set(CRITERIA)
    if unknown:
        raise ValueError(f"Unknown minimum score criterion: {sorted(unknown)[0]}")
    for key in ("tasks", "industry", "skills"):
        if key in supplied:
            value = supplied[key]
            if isinstance(value, bool) or value not in (1, 2, 3):
                raise ValueError(f"Minimum for {key} must be 1, 2 or 3")
            effective_minimums[key] = value

    payload = {
        "job": job.model_dump(mode="json"),
        "profile": _payload(profile),
        "resumes": [_payload(resume) for resume in resumes],
    }

    analysis = await gateway.structured("resume_analyst", payload, ResumeAnalysis)

    # Keep the project's deterministic safety layer unchanged.
    analysis = _ground_resume_analysis(analysis, job, profile, resumes)
    rows = _assessment_rows(job, analysis, effective_minimums)
    weighted_total = sum(
        Decimal(row.raw_points) / Decimal(row.raw_max_points) * Decimal(row.max_points)
        for row in rows
    )
    score = _round_half_up(weighted_total)

    assessments = [
        analysis.title,
        analysis.tasks,
        analysis.industry,
        analysis.required_years,
        analysis.languages,
        analysis.skills,
    ]
    reason = analysis.reason.strip() or "Оценка вакансии на основе резюме."

    minimum_score_violations = []
    for key, minimum in effective_minimums.items():
        if key not in CRITERIA:
            raise ValueError(f"Unknown minimum score criterion: {key}")
        raw_max = CRITERIA[key][0]
        if not 0 <= minimum <= raw_max:
            raise ValueError(f"Minimum for {key} must be between 0 and {raw_max}")
        actual = getattr(analysis, key).score
        if actual < minimum:
            minimum_score_violations.append(f"{key}: {actual}/{raw_max}, минимум {minimum}")
    blocked = bool(minimum_score_violations)
    if blocked:
        reason = f"{reason} Не достигнут минимум: {'; '.join(minimum_score_violations)}"

    return JobEvaluation(
        decision="apply" if not blocked else "skip",
        score=score,
        confidence=max((item.confidence for item in assessments), default=0),
        category=analysis.category or job.title,
        score_breakdown=rows,
        minimum_score_violations=minimum_score_violations,
        hard_rule_violations=[f"minimum_score:{item}" for item in minimum_score_violations],
        reason=reason,
        has_test_assignment=bool(job.has_test_assignment),
    )
