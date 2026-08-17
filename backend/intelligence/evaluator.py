from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from backend.config import settings
from backend.schemas.domain import (
    JobEvaluation,
    JobPosting,
    MatchAssessment,
    ResumeAnalysis,
    ScoreComponent,
)

from .antigravity import analyze_relevance
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


def _points(assessment: MatchAssessment, maximum: int) -> int:
    if maximum <= 0:
        return 0
    return round(maximum * min(max(assessment.match, 0), 1))


def _assessment_rows(
    job: JobPosting, analysis: ResumeAnalysis
) -> list[ScoreComponent]:
    has_level = analysis.vacancy_seniority is not None
    criteria = {
        "title": (analysis.title, 5),
        "tasks": (analysis.tasks, 30),
        "industry": (analysis.industry, 25),
        "required_years": (analysis.required_years, 10 if has_level else 20),
        "seniority": (analysis.seniority, 10 if has_level else 0),
        "languages": (analysis.languages, 10),
        "skills": (analysis.skills, 10),
    }
    titles = {
        "title": "Название должности",
        "tasks": "Задачи",
        "industry": "Сфера",
        "required_years": "Годы опыта",
        "seniority": "Уровень позиции",
        "languages": "Языки",
        "skills": "Навыки",
    }
    return [
        ScoreComponent(
            key=key,
            title=titles[key],
            points=_points(assessment, maximum),
            max_points=maximum,
            explanation=assessment.explanation
            or f"Совпадение по критерию: {assessment.match:.0%}",
            evidence=list(assessment.evidence),
        )
        for key, (assessment, maximum) in criteria.items()
    ]


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
_SENIORITY_PATTERNS = {
    "junior": r"(?<![\w-])(?:junior|jr\.?|джун\w*|младш\w*)(?![\w-])",
    "middle": r"(?<![\w-])(?:middle|мидл\w*|миддл\w*)(?![\w-])",
    "senior": r"(?<![\w-])(?:senior|sr\.?|сеньор\w*|старш\w*)(?![\w-])",
}


def _explicit_vacancy_seniority(job: JobPosting) -> str | None:
    text = " ".join(
        [job.title, job.description, *job.responsibilities, *job.required_skills]
    )
    detected = [
        level
        for level, pattern in _SENIORITY_PATTERNS.items()
        if re.search(pattern, text, re.I)
    ]
    return detected[0] if len(detected) == 1 else None


def _validate_vacancy_seniority(analysis: ResumeAnalysis, job: JobPosting) -> None:
    explicit_level = _explicit_vacancy_seniority(job)
    if explicit_level is not None and analysis.vacancy_seniority == explicit_level:
        return
    analysis.vacancy_seniority = explicit_level
    analysis.seniority.match = 0
    analysis.seniority.confidence = 0
    analysis.seniority.evidence = []
    analysis.seniority.explanation = (
        "Уровень позиции не подтверждён явным junior/middle/senior маркером вакансии."
        if explicit_level is None
        else "Модельный уровень позиции не совпал с явным маркером вакансии."
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
    job_source = " ".join(
        [job.title, job.description, *job.responsibilities, *job.required_skills]
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
        "seniority",
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
        if assessment.match > 0 and not grounded:
            assessment.match = 0
            assessment.confidence = 0
            assessment.explanation = (
                "Совпадение обнулено: evidence не подтверждено входными данными."
            )
            assessment.evidence = []
    _validate_vacancy_seniority(analysis, job)
    foreign_requirements = _language_requirement_evidence(job, _FOREIGN_LANGUAGE)
    if not foreign_requirements:
        russian_requirements = _language_requirement_evidence(job, _RUSSIAN_LANGUAGE)
        analysis.languages.match = 1
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
    score_threshold: int,
    gateway: ModelGateway,
) -> JobEvaluation:
    """Evaluate a vacancy only against selected resumes."""
    if not 0 <= score_threshold <= 100:
        raise ValueError("score_threshold must be between 0 and 100")

    payload = {
        "job": job.model_dump(mode="json"),
        "profile": _payload(profile),
        "resumes": [_payload(resume) for resume in resumes],
    }

    # Only relevance analysis is moved to Antigravity.
    # All other LLM roles continue using the normal ModelGateway/Ollama.
    if settings.relevance_provider == "antigravity":
        analysis = await analyze_relevance(payload, ResumeAnalysis)
    else:
        analysis = await gateway.structured("resume_analyst", payload, ResumeAnalysis)

    # Keep the project's deterministic safety layer unchanged.
    analysis = _ground_resume_analysis(analysis, job, profile, resumes)
    rows = _assessment_rows(job, analysis)
    score = sum(item.points for item in rows)

    assessments = [
        analysis.title,
        analysis.tasks,
        analysis.industry,
        analysis.required_years,
        analysis.seniority,
        analysis.languages,
        analysis.skills,
    ]
    reason = analysis.reason.strip() or "Оценка вакансии на основе резюме."

    return JobEvaluation(
        decision="apply" if score >= score_threshold else "skip",
        score=score,
        confidence=max((item.confidence for item in assessments), default=0),
        category=analysis.category or job.title,
        score_breakdown=rows,
        reason=reason,
        has_test_assignment=bool(job.has_test_assignment),
        flag_filter=None,
    )
