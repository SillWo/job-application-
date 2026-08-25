from __future__ import annotations

import re
from collections.abc import Sequence
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from backend.schemas.domain import (
    DesiredJobPolicy,
    FlagMatch,
    JobEvaluation,
    JobPosting,
    MatchAssessment,
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
    "tasks": (4, 35, "Задачи"), "skills": (2, 20, "Навыки"),
    "experience_depth": (4, 15, "Годы опыта"), "role_match": (4, 10, "Роль"),
    "industry": (4, 10, "Сфера"), "special_requirements": (2, 10, "Особые требования"),
}

# Primary-score gates; special_requirements remains fixed at 1.
DEFAULT_MINIMUM_SCORES = {
    "tasks": 2,
    "industry": 2,
    "skills": 1,
    "experience_depth": 1, "role_match": 1, "special_requirements": 1,
}
FLAG_CONFIDENCE_THRESHOLD = 0.70
_SPECIAL_REQUIREMENT = re.compile(
    r"образован|высш\w*|диплом|сертифик|certificat|английск|english|иностранн\w*\s+язык|"
    r"гражданств|водительск\w*\s+прав|лицензи",
    re.I,
)


def _default_human_explanation(score: int, raw_max: int) -> str:
    """Fallback copy for the UI; never expose the internal scoring scale."""
    if score <= 0:
        return "По этому критерию соответствие не подтверждено."
    if score >= raw_max:
        return "По этому критерию соответствие хорошее."
    return "По этому критерию есть частичное соответствие."


def _round_half_up(value: Decimal) -> int:
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _skill_primary_score(assessments: Sequence[Any]) -> int:
    total = sum(
        Decimal(item.score) * (Decimal(2) if item.importance == "required" else Decimal(1))
        for item in assessments
    )
    divisor = sum(
        Decimal(2) if item.importance == "required" else Decimal(1)
        for item in assessments
    )
    return _round_half_up(total / divisor) if divisor else 0


def _assessment_rows(
    job: JobPosting, analysis: ResumeAnalysis, minimum_scores: dict[str, int] | None = None
) -> list[ScoreComponent]:
    rows = []
    criteria = CRITERIA
    for key, (raw_max, weight, title) in criteria.items():
        assessment = getattr(analysis, key)
        if key == "skills" and isinstance(assessment, list):
            assessment = MatchAssessment(
                score=_skill_primary_score(assessment),
                explanation=analysis.skills_summary,
                evidence=[evidence for item in assessment for evidence in item.evidence],
            )
        if assessment.score > raw_max:
            raise ValueError(f"{key}.score must be between 0 and {raw_max}")
        weighted = Decimal(assessment.score) / Decimal(raw_max) * Decimal(weight)
        rows.append(ScoreComponent(
            key=key, title=title, points=_round_half_up(weighted), max_points=weight,
            raw_points=assessment.score, raw_max_points=raw_max,
            minimum_points=(minimum_scores or {}).get(key),
            minimum_failed=(key in (minimum_scores or {}) and assessment.score < minimum_scores[key]),
            explanation=assessment.explanation or _default_human_explanation(assessment.score, raw_max),
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


def _has_explicit_special_requirements(job: JobPosting) -> bool:
    source = " ".join([job.description, *job.required_skills, *job.optional_skills])
    return bool(_SPECIAL_REQUIREMENT.search(source))


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
    fields = ("tasks", "experience_depth", "role_match", "industry", "special_requirements")
    def human_fallback(score: int, maximum: int) -> str:
        if score <= 0:
            return "Соответствие по этому критерию не подтверждено."
        if score >= maximum:
            return "По этому критерию соответствие хорошее."
        return "По этому критерию есть частичное соответствие."

    for field_name in fields:
        assessment = getattr(analysis, field_name)
        if isinstance(assessment, list):
            continue
        grounded_evidence = []
        for quote in assessment.evidence:
            normalized = str(quote).strip()
            if re.match(r"^вакансия\s*:", normalized, re.I):
                source = job_source
            elif re.match(r"^(?:резюме|ревюме|профиль)\s*:", normalized, re.I):
                source = resume_source
            else:
                source = combined_source
            if _quote_grounded(normalized, source):
                grounded_evidence.append(quote)
        assessment.evidence = grounded_evidence
        if assessment.score > 0 and not grounded_evidence:
            assessment.confidence = min(assessment.confidence, 0.5)
            assessment.explanation = assessment.explanation.strip() or human_fallback(assessment.score, CRITERIA[field_name][0])
    for item in analysis.skills:
        grounded_evidence = [
            quote for quote in item.evidence
            if _quote_grounded(str(quote).strip(), resume_source)
        ]
        item.evidence = grounded_evidence
        if item.score > 0 and not grounded_evidence:
            item.explanation = item.explanation.strip() or human_fallback(item.score, 2)
    if not _has_explicit_special_requirements(job):
        analysis.special_requirements = MatchAssessment(
            score=2,
            confidence=1,
            evidence=[],
            explanation="В вакансии нет явных требований к образованию, сертификатам или языкам.",
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
    preference_policy: DesiredJobPolicy | dict | None = None,
) -> JobEvaluation:
    """Evaluate a vacancy only against selected resumes."""
    effective_minimums = dict(DEFAULT_MINIMUM_SCORES)
    supplied = minimum_scores or {}
    configurable = {"tasks", "industry", "skills", "experience_depth", "role_match"}
    legacy_keys = {"title", "required_years", "languages", "work_conditions"}
    unknown = set(supplied) - configurable - legacy_keys - {"special_requirements"}
    if unknown:
        raise ValueError(f"Unknown minimum score criterion: {sorted(unknown)[0]}")
    if "special_requirements" in supplied and supplied["special_requirements"] != 1:
        raise ValueError("Minimum for special_requirements must be 1")
    for key in configurable:
        if key in supplied:
            value = supplied[key]
            maximum = 2 if key == "skills" else 4
            if isinstance(value, bool) or value not in range(1, maximum + 1):
                raise ValueError(f"Minimum for {key} must be 1..{maximum}")
            effective_minimums[key] = value

    payload = {
        "job": job.model_dump(mode="json"),
        "profile": _payload(profile),
        "resumes": [_payload(resume) for resume in resumes],
    }
    if preference_policy:
        payload["preference_policy"] = _payload(preference_policy)

    analysis = await gateway.structured("resume_analyst", payload, ResumeAnalysis)

    # Keep the project's deterministic safety layer unchanged.
    analysis = _ground_resume_analysis(analysis, job, profile, resumes)
    policy = DesiredJobPolicy.model_validate(preference_policy or {})
    if preference_policy:
        payload["preference_policy"] = policy.model_dump(mode="json")
    job_text = " ".join([job.title, job.description, *job.responsibilities, *job.required_skills]).casefold()
    known = {flag.id: flag for flag in [*policy.green_flags, *policy.red_flags]}
    matches: list[FlagMatch] = []
    seen_ids: set[str] = set()
    for match in analysis.flag_matches:
        if match.flag_id not in known or match.flag_id in seen_ids:
            continue
        seen_ids.add(match.flag_id)
        evidence = [str(item) for item in match.evidence if _quote_grounded(str(item), job_text)]
        matches.append(match.model_copy(update={"matched": bool(match.matched and match.confidence >= FLAG_CONFIDENCE_THRESHOLD and evidence), "evidence": evidence}))
    for flag_id in known:
        if flag_id not in seen_ids:
            matches.append(FlagMatch(flag_id=flag_id))
    analysis.flag_matches = matches
    red_hit = any(item.matched and item.flag_id in {flag.id for flag in policy.red_flags} for item in matches)
    salary_hit = False
    if policy.desired_salary and job.salary and job.salary.currency.casefold() == policy.desired_salary.currency.casefold():
        offered = job.salary.maximum or job.salary.minimum
        salary_hit = offered is not None and offered <= policy.desired_salary.minimum_monthly_amount * 0.75
    task_hit = any(item.matched and item.flag_id in {flag.id for flag in policy.green_flags if flag.category == "desired_task"} for item in matches)
    if task_hit:
        analysis.tasks.score = min(4, analysis.tasks.score + 1)
    rows = _assessment_rows(job, analysis, effective_minimums)
    weighted_total = sum(
        Decimal(row.raw_points) / Decimal(row.raw_max_points) * Decimal(row.max_points)
        for row in rows
    )
    score = _round_half_up(weighted_total)

    assessments = [analysis.tasks, analysis.experience_depth, analysis.role_match, analysis.industry, analysis.special_requirements]
    reason = analysis.reason.strip() or "Оценка вакансии на основе резюме."

    minimum_score_violations = []
    for key, minimum in effective_minimums.items():
        if key not in CRITERIA:
            raise ValueError(f"Unknown minimum score criterion: {key}")
        raw_max = CRITERIA[key][0]
        if not 0 <= minimum <= raw_max:
            raise ValueError(f"Minimum for {key} must be between 0 and {raw_max}")
        actual = (
            _skill_primary_score(analysis.skills)
            if key == "skills"
            else getattr(analysis, key).score
        )
        if actual < minimum:
            minimum_score_violations.append(f"{key}: {actual}/{raw_max}, минимум {minimum}")
    blocked = bool(minimum_score_violations or red_hit or salary_hit)
    # Keep the detailed violations in their dedicated internal fields, while the
    # reason shown in the vacancies UI stays short and understandable.
    if blocked:
        blockers = []
        if minimum_score_violations:
            blockers.append("по отдельным важным критериям совпадения недостаточно")
        if red_hit:
            blockers.append("вакансия не соответствует описанию желаемой работы")
        if salary_hit:
            blockers.append("условия оплаты не соответствуют ожиданиям")
        reason = f"{reason.rstrip('.')} Вакансия не рекомендована: {', '.join(blockers)}."
    else:
        reason = f"{reason.rstrip('.')} Вакансия подходит для отклика."

    return JobEvaluation(
        decision="apply" if not blocked else "skip",
        score=score,
        confidence=max((item.confidence for item in assessments), default=0),
        category=analysis.category or job.title,
        score_breakdown=rows,
        minimum_score_violations=minimum_score_violations,
        hard_rule_violations=[f"minimum_score:{item}" for item in minimum_score_violations]
        + (["preference_red_flag"] if red_hit else [])
        + (["salary_below_preference"] if salary_hit else []),
        flag_matches=matches,
        reason=reason,
        has_test_assignment=bool(job.has_test_assignment),
    )
