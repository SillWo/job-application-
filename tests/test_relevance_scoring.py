from __future__ import annotations

import pytest
from pydantic import ValidationError

from backend.intelligence import evaluator
from backend.intelligence.gateway import _resume_analysis_missing_fields
from backend.intelligence.prompts import ROLE_PROMPTS
from backend.schemas.domain import (
    JobPosting,
    MatchAssessment,
    ResumeAnalysis,
    SkillAssessment,
)

CRITERIA = (
    "tasks", "skills", "experience_depth", "role_match",
    "industry", "special_requirements",
)


class Gateway:
    def __init__(self, analysis: ResumeAnalysis):
        self.analysis = analysis

    async def structured(self, role, payload, schema):
        self.role = role
        self.payload = payload
        self.schema = schema
        return self.analysis.model_copy(deep=True)


def assessment(score: int, evidence: str = "Python разработчик") -> MatchAssessment:
    return MatchAssessment(
        score=score,
        confidence=0.8,
        evidence=[evidence] if score else [],
        explanation="Подтверждено входными данными",
    )


def skill(name: str, score: int, importance: str = "required") -> SkillAssessment:
    return SkillAssessment(
        skill=name,
        importance=importance,
        score=score,
        evidence=[name] if score else [],
        explanation="Подтверждено резюме" if score else "Подтверждения нет",
    )


def analysis(*, skills: list[SkillAssessment] | None = None, **scores: int) -> ResumeAnalysis:
    values = {
        "tasks": 0,
        "experience_depth": 0,
        "role_match": 0,
        "industry": 0,
        "special_requirements": 0,
    }
    values.update(scores)
    return ResumeAnalysis(
        **{key: assessment(value) for key, value in values.items()},
        skills=skills or [],
    )


def test_resume_analyst_prompt_contains_complete_discrete_rubric():
    prompt = ROLE_PROMPTS["resume_analyst"]
    required_instructions = (
        "tasks 0..4",
        "required вес 2, preferred вес 1",
        "отсутствие подтверждения не означает",
        "experience_depth 0..4",
        "role_match 0..4",
        "не названию",
        "industry 0..4",
        "special_requirements 0..2",
        "без total score",
    )
    assert all(instruction in prompt for instruction in required_instructions)
    assert "title 0.." not in prompt
    assert "required_years" not in prompt
    assert "languages.score" not in prompt
    assert "2–3 коротких предложения" in prompt
    assert "о чём вакансия" in prompt
    assert "главное конкретное несоответствие" in prompt
    assert "отклик отправлен" in prompt
    assert "Не утверждай, что отклик отправлен: на этапе оценки это неизвестно." in prompt


def test_resume_analysis_rejects_old_six_criterion_contract():
    with pytest.raises(ValidationError):
        ResumeAnalysis.model_validate({
            "title": {}, "tasks": {}, "industry": {},
            "required_years": {}, "languages": {}, "skills": {},
        })


def test_resume_analysis_requires_grounding_for_positive_match():
    result = analysis()
    result.role_match = MatchAssessment(
        score=1, confidence=0, evidence=[], explanation="",
    )
    assert _resume_analysis_missing_fields(result) == ["reason", "skills_summary", "role_match.grounding"]


@pytest.mark.parametrize("field_name", ["experience_depth", "industry", "role_match"])
def test_grounding_keeps_partial_scalar_evidence_and_score(job, field_name):
    result = analysis(**{field_name: 3})
    assessment_result = getattr(result, field_name)
    assessment_result.evidence = ["Вакансия: Python разработчик", "несуществующая цитата"]

    grounded = evaluator._ground_resume_analysis(result, job, {}, [])

    assessment_result = getattr(grounded, field_name)
    assert assessment_result.score == 3
    assert assessment_result.confidence == 0.8
    assert assessment_result.evidence == ["Вакансия: Python разработчик"]
    assert "локальную лексическую проверку" not in assessment_result.explanation


@pytest.mark.parametrize("field_name", ["experience_depth", "industry", "role_match"])
def test_grounding_keeps_scalar_score_with_warning_when_evidence_is_invalid(job, field_name):
    result = analysis(**{field_name: 3})
    getattr(result, field_name).evidence = ["полностью несвязанная цитата"]

    grounded = evaluator._ground_resume_analysis(result, job, {}, [])

    assessment_result = getattr(grounded, field_name)
    assert assessment_result.score == 3
    assert assessment_result.confidence == 0.5
    assert assessment_result.evidence == []
    assert "Evidence" not in assessment_result.explanation
    assert "score" not in assessment_result.explanation


def test_grounding_keeps_skill_score_and_filters_invalid_evidence(job):
    result = analysis(skills=[skill("SQL", 2)])
    result.skills[0].evidence = ["SQL", "несуществующая технология"]

    grounded = evaluator._ground_resume_analysis(result, job, {}, [{"skills": ["SQL"]}])

    assert grounded.skills[0].score == 2
    assert grounded.skills[0].evidence == ["SQL"]

    result = analysis(skills=[skill("SQL", 2)])
    result.skills[0].evidence = ["несуществующая технология"]
    grounded = evaluator._ground_resume_analysis(result, job, {}, [{"skills": ["SQL"]}])
    assert grounded.skills[0].score == 2
    assert grounded.skills[0].evidence == []
    assert "Evidence" not in grounded.skills[0].explanation
    assert "score" not in grounded.skills[0].explanation


@pytest.mark.asyncio
async def test_minimum_gate_does_not_fail_only_on_rejected_evidence(job):
    result = await evaluator.evaluate(
        job, {}, [], Gateway(analysis(experience_depth=3)),
        minimum_scores={"experience_depth": 3},
    )

    assert result.score_breakdown[2].minimum_failed is False
    assert not any(item.startswith("experience_depth:") for item in result.minimum_score_violations)


@pytest.fixture
def job() -> JobPosting:
    return JobPosting(
        source="mock",
        url="https://example.test/1",
        title="Python разработчик",
        description="Python разработчик, SQL, Jira. Высшее образование. Удалённая работа.",
        required_skills=["SQL", "Jira"],
        optional_skills=["Python"],
        work_format="Удалённо",
    )


@pytest.mark.asyncio
async def test_weighted_formula_skill_average_and_breakdown(job):
    model_result = analysis(
        tasks=3,
        experience_depth=3,
        role_match=2,
        industry=2,
        special_requirements=1,
        skills=[skill("SQL", 2), skill("Jira", 0), skill("Python", 1, "preferred")],
    )
    result = await evaluator.evaluate(
        job, {}, [{"skills": ["SQL", "Python"]}], Gateway(model_result),
    )

    # 26.25 + 10 + 11.25 + 5 + 5 = 57.5 -> 58.
    assert result.score == 63
    assert [row.key for row in result.score_breakdown] == list(CRITERIA)
    assert [row.max_points for row in result.score_breakdown] == [35, 20, 15, 10, 10, 10]
    assert result.score_breakdown[1].raw_points == 1
    assert result.score_breakdown[1].raw_max_points == 2
    assert result.decision == "apply"
    assert [row.minimum_points for row in result.score_breakdown] == [2, 1, 1, 1, 2, 1]
    assert not any(row.minimum_failed for row in result.score_breakdown)


@pytest.mark.asyncio
async def test_required_skill_average_uses_half_up_rounding(job):
    result = await evaluator.evaluate(
        job,
        {},
        [{"skills": ["SQL", "Jira"]}],
        Gateway(analysis(
            tasks=2,
            experience_depth=1,
            role_match=1,
            industry=2,
            special_requirements=1,
            skills=[skill("SQL", 2), skill("Jira", 1)],
        )),
    )
    skills_row = next(row for row in result.score_breakdown if row.key == "skills")
    assert skills_row.raw_points == 2  # (2*2 + 1*2) / 4 = 1.5 -> 2
    assert skills_row.points == 20


@pytest.mark.asyncio
async def test_empty_skill_requirements_score_zero(job):
    result = await evaluator.evaluate(job, {}, [], Gateway(analysis()))
    skills_row = next(row for row in result.score_breakdown if row.key == "skills")
    assert skills_row.raw_points == 0
    assert skills_row.points == 0


@pytest.mark.asyncio
async def test_absent_conditions_and_special_requirements_do_not_penalize():
    plain_job = JobPosting(
        source="mock",
        url="https://example.test/plain",
        title="Редактор",
        description="Редактировать тексты",
    )
    result = await evaluator.evaluate(plain_job, {}, [], Gateway(analysis()))
    rows = {row.key: row for row in result.score_breakdown}
    assert rows["special_requirements"].raw_points == 2


@pytest.mark.asyncio
async def test_default_gates_reject_missing_criteria(job):
    result = await evaluator.evaluate(job, {}, [], Gateway(analysis()))
    assert result.decision == "skip"
    assert {"tasks", "skills", "experience_depth", "role_match", "industry", "special_requirements"} == {
        item.split(":", 1)[0] for item in result.minimum_score_violations
    }


@pytest.mark.asyncio
async def test_total_uses_mathematical_half_up_rounding(job):
    result = await evaluator.evaluate(
        job,
        {},
        [{"skills": ["SQL"]}],
        Gateway(analysis(
            tasks=1,
            experience_depth=1,
            role_match=1,
            industry=1,
            special_requirements=1,
            skills=[skill("SQL", 1)],
        )),
        minimum_scores={
            "tasks": 1, "skills": 1, "experience_depth": 1,
            "role_match": 1, "industry": 1,
        },
    )
    # 8.75 + 10 + 3.75 + 2.5 + 5 = 30.
    assert result.score == 33
    assert result.decision == "apply"


@pytest.mark.asyncio
async def test_minimum_score_blocks_even_when_total_passes(job):
    result = await evaluator.evaluate(
        job,
        {},
        [{"skills": ["SQL"]}],
        Gateway(analysis(
            tasks=2,
            experience_depth=4,
            role_match=4,
            industry=4,
            special_requirements=2,
            skills=[skill("SQL", 2)],
        )),
        minimum_scores={"tasks": 3},
    )
    assert result.score == 83
    assert result.decision == "skip"
    assert result.minimum_score_violations == ["tasks: 2/4, минимум 3"]
    assert result.score_breakdown[0].minimum_failed is True
    assert result.reason.endswith(
        "Вакансия не рекомендована: по отдельным важным критериям совпадения недостаточно."
    )
    for technical in ("tasks", "/", "минимум", "score", "confidence", "evidence"):
        assert technical not in result.reason


@pytest.mark.asyncio
async def test_legacy_session_minimum_keys_are_ignored(job):
    result = await evaluator.evaluate(
        job,
        {},
        [{"skills": ["SQL"]}],
        Gateway(analysis(
            tasks=2,
            experience_depth=1,
            role_match=1,
            industry=2,
            special_requirements=1,
            skills=[skill("SQL", 1)],
        )),
        minimum_scores={"title": 2, "required_years": 0, "languages": 0},
    )
    assert result.decision == "apply"
    assert result.minimum_score_violations == []


@pytest.mark.asyncio
async def test_work_conditions_legacy_minimum_is_ignored(job):
    result = await evaluator.evaluate(job, {}, [], Gateway(analysis()), minimum_scores={"work_conditions": 1})
    assert result.decision == "skip"


@pytest.mark.asyncio
async def test_payload_contains_all_six_criteria_and_no_total(job):
    gateway = Gateway(analysis())
    await evaluator.evaluate(job, {}, [], gateway)
    assert gateway.role == "resume_analyst"
    assert gateway.schema is ResumeAnalysis
    assert set(gateway.payload) == {"job", "profile", "resumes"}
    assert set(CRITERIA) <= set(gateway.schema.model_fields)
    assert "score" not in gateway.schema.model_fields
