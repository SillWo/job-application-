from __future__ import annotations

import pytest

from backend.intelligence import evaluator
from backend.intelligence.gateway import _resume_analysis_missing_fields
from backend.intelligence.prompts import ROLE_PROMPTS
from backend.schemas.domain import JobPosting, MatchAssessment, ResumeAnalysis


class Gateway:
    def __init__(self, analysis: ResumeAnalysis):
        self.analysis = analysis

    async def structured(self, role, payload, schema):
        return self.analysis.model_copy(deep=True)


def assessment(score: int) -> MatchAssessment:
    return MatchAssessment(
        score=score,
        confidence=0.8,
        evidence=["Вакансия: Python разработчик"],
        explanation="Подтверждено входными данными",
    )


def analysis(**scores: int) -> ResumeAnalysis:
    values = {"title": 0, "tasks": 0, "industry": 0, "required_years": 0, "languages": 0, "skills": 0}
    values.update(scores)
    return ResumeAnalysis(**{key: assessment(value) for key, value in values.items()})


def test_resume_analyst_prompt_contains_complete_discrete_rubric():
    prompt = ROLE_PROMPTS["resume_analyst"]
    required_instructions = (
        "A title: 2",
        "B tasks: 3",
        "C industry:",
        "B2C/B2B",
        "4 — сфера совпадает и профиль задач совпадает",
        "D required_years:",
        "именно минимально требуемые вакансией годы",
        "не более чем на один год",
        "E languages: 2",
        "ровно на один уровень CEFR",
        "F skills:",
        "отсутствует не более двух",
        "отсутствует более двух и они не смежны",
    )
    assert all(instruction in prompt for instruction in required_instructions)


def test_resume_analyst_prompt_explicitly_exempts_unrequired_foreign_language():
    prompt = ROLE_PROMPTS["resume_analyst"]

    assert "languages.score=2, languages.confidence=1 и languages.evidence=[]" in prompt
    assert "Для всех остальных критериев и для явного требования иностранного языка" in prompt


def test_resume_analysis_allows_ungrounded_language_score_for_deterministic_grounding():
    result = analysis()
    result.languages = MatchAssessment(score=2, confidence=0, evidence=[])

    assert _resume_analysis_missing_fields(result) == []


def test_resume_analysis_still_requires_grounding_for_other_criteria():
    result = analysis()
    result.title = MatchAssessment(score=1, confidence=0, evidence=[])

    assert _resume_analysis_missing_fields(result) == ["title.grounding"]


def test_resume_analysis_still_requires_language_nested_fields():
    result = analysis()
    result.languages = MatchAssessment(score=2)

    assert _resume_analysis_missing_fields(result) == [
        "languages.confidence",
        "languages.evidence",
    ]


@pytest.fixture
def job() -> JobPosting:
    return JobPosting(source="mock", url="https://example.test/1", title="Python разработчик", description="Python разработчик")


@pytest.mark.asyncio
async def test_weighted_discrete_formula_and_breakdown(job):
    model_result = analysis(title=1, tasks=2, industry=3, required_years=1, languages=1, skills=2)
    result = await evaluator.evaluate(job, {}, [], 70, Gateway(model_result))

    assert result.score == 68  # no explicit language requirement forces E=2
    assert [row.key for row in result.score_breakdown] == ["title", "tasks", "industry", "required_years", "languages", "skills"]
    assert result.score_breakdown[0].raw_points == 1
    assert result.score_breakdown[0].raw_max_points == 2
    assert result.decision == "skip"


@pytest.mark.asyncio
async def test_minimum_scores_are_disabled_by_default(job):
    result = await evaluator.evaluate(job, {}, [], 0, Gateway(analysis()), minimum_scores=None)
    assert result.decision == "apply"
    assert result.minimum_score_violations == []


@pytest.mark.asyncio
async def test_total_uses_mathematical_half_up_rounding(job):
    result = await evaluator.evaluate(job, {}, [], 13, Gateway(analysis(title=1)))
    assert result.score == 13  # title 2.5 + default language 10 = 12.5
    assert result.decision == "apply"


@pytest.mark.asyncio
async def test_minimum_score_blocks_even_when_total_passes(job):
    result = await evaluator.evaluate(
        job, {}, [], 50,
        Gateway(analysis(title=0, tasks=3, industry=4, required_years=2, languages=2, skills=3)),
        minimum_scores={"title": 1},
    )
    assert result.score == 95
    assert result.decision == "skip"
    assert result.minimum_score_violations == ["title: 0/2, минимум 1"]
    assert result.score_breakdown[0].minimum_failed is True
