import pytest

from backend.intelligence.evaluator import evaluate
from backend.intelligence.gateway import _schema_without_preference_matches, _system_prompt_for_role
from backend.intelligence.preference_policy import _normalize, compile_preference_policy
from backend.intelligence.search_planner import plan_search_queries
from backend.schemas.domain import (
    DesiredJobPolicy,
    FlagMatch,
    JobPosting,
    MatchAssessment,
    PreferenceFlag,
    ResumeAnalysis,
    Salary,
    SalaryPreference,
    SkillAssessment,
)


def test_grouped_compiler_shape_is_normalized_with_stable_ids():
    policy = DesiredJobPolicy.model_validate({
        "green_flags": {"desired_industries": ["GameDev"], "tasks": [{"text": "игровые механики", "ignored": "x"}], "desired_salary": ["не зарплата"]},
        "red_flags": {"other": ["продажи"], "desired_industry": ["финансы"]},
        "desired_salary": {"minimum": 200000, "currency": "RUB", "ignored": True},
    })
    policy = _normalize(policy)
    assert [item.id for item in policy.green_flags] == ["green-1", "green-2", "green-3"]
    assert [item.category for item in policy.green_flags] == ["desired_industry", "desired_task", "desired_salary"]
    assert [item.category for item in policy.red_flags] == ["other", "desired_industry"]
    assert policy.desired_salary.minimum_monthly_amount == 200000


@pytest.mark.asyncio
async def test_compiler_discards_hallucinated_salary_without_source_marker():
    class Gateway:
        async def structured(self, role, payload, schema):
            return DesiredJobPolicy(
                green_flags=[PreferenceFlag(id="x", text="зарплата 200000", category="desired_salary")],
                desired_salary=SalaryPreference(minimum_monthly_amount=200000),
            )

    result = await compile_preference_policy(Gateway(), "Интересует разработка игровых механик")
    assert result.desired_salary is None
    assert all(flag.category != "desired_salary" for flag in result.green_flags)


def test_empty_preference_schema_has_no_flag_contract():
    schema = _schema_without_preference_matches("resume_analyst", ResumeAnalysis, {})
    rendered = str(schema)
    assert "flag_matches" not in rendered
    assert "FlagMatch" not in rendered


def test_preference_schema_requires_complete_flag_matches():
    payload = {"preference_policy": policy(red=True).model_dump(mode="json")}
    schema = _schema_without_preference_matches("resume_analyst", ResumeAnalysis, payload)
    assert "flag_matches" in schema["required"]
    assert set(schema["$defs"]["FlagMatch"]["required"]) == {
        "flag_id", "matched", "confidence", "evidence", "explanation",
    }


class FakeGateway:
    def __init__(self, analysis):
        self.analysis = analysis
        self.payloads = []

    async def structured(self, role, payload, schema):
        self.payloads.append((role, payload))
        if role == "resume_analyst":
            return self.analysis
        if role == "search_planner":
            from backend.intelligence.search_planner import SearchQuery, SearchQueryPlan
            policy = payload.get("preference_policy", {})
            return SearchQueryPlan(queries=[SearchQuery(query=f["text"], relation_to_resume="user preference", is_title_equivalent=False) for f in policy.get("green_flags", []) if f["category"] == "desired_industry"])
        raise AssertionError(role)


def policy(*, red=False, task=False, industry=False, salary=None):
    green = []
    if task: green.append(PreferenceFlag(id="green-task", text="автоматизация задач", category="desired_task"))
    if industry: green.append(PreferenceFlag(id="green-industry", text="GameDev", category="desired_industry"))
    return DesiredJobPolicy(
        green_flags=green,
        red_flags=[PreferenceFlag(id="red-1", text="продажи", category="other")] if red else [],
        desired_salary=SalaryPreference(minimum_monthly_amount=100_000) if salary else None,
    )


def assessment(score=0, confidence=0, evidence=None):
    return MatchAssessment(score=score, confidence=confidence, evidence=evidence or [], explanation="x")


def analysis(matches=None, task_score=1):
    return ResumeAnalysis(
        tasks=assessment(task_score, .8, ["автоматизация задач"]),
        skills=[],
        experience_depth=assessment(),
        role_match=assessment(),
        industry=assessment(),
        special_requirements=assessment(),
        flag_matches=matches or [],
    )


def job(salary=None, text="Вакансия: автоматизация задач"):
    return JobPosting(source="hh", url="https://hh.ru/vacancy/1", title="Инженер", description=text, salary=salary)


@pytest.mark.asyncio
@pytest.mark.parametrize("confidence, expected", [(0.69, False), (0.70, True)])
async def test_red_threshold(confidence, expected):
    p = policy(red=True)
    gateway = FakeGateway(analysis([FlagMatch(flag_id="red-1", matched=True, confidence=confidence, evidence=["продажи"])]))
    result = await evaluate(job(text="Продажи и работа с клиентами"), {}, [{}], gateway, preference_policy=p)
    assert ("preference_red_flag" in result.hard_rule_violations) is expected
    if expected:
        assert result.decision == "skip"


@pytest.mark.asyncio
async def test_unverified_red_match_is_skipped_automatically():
    p = policy(red=True)
    complete = analysis(
        [FlagMatch(flag_id="red-1", matched=True)],
        task_score=2,
    )
    complete.experience_depth = assessment(1, .8, ["Продажи"])
    complete.role_match = assessment(1, .8, ["Продажи"])
    complete.industry = assessment(2, .8, ["Продажи"])
    complete.special_requirements = assessment(1, .8, ["Продажи"])
    complete.skills = [SkillAssessment(skill="product", importance="required", score=1, evidence=["product"], explanation="x")]
    result = await evaluate(
        job(text="Продажи и работа с клиентами"), {}, [{}], FakeGateway(complete), preference_policy=p,
    )
    assert result.decision == "skip"
    assert result.requires_manual_review is False
    assert "preference_red_flag_unverified" in result.hard_rule_violations


@pytest.mark.asyncio
async def test_hiring_format_is_grounded_for_red_flags():
    p = DesiredJobPolicy(
        red_flags=[PreferenceFlag(id="red-1", text="ГПХ", category="other")],
    )
    matches = [FlagMatch(flag_id="red-1", matched=True, confidence=1, evidence=["Оформление: Договор ГПХ"])]
    complete = analysis(matches, task_score=2)
    for field in ("experience_depth", "role_match", "industry", "special_requirements"):
        setattr(complete, field, assessment(1 if field != "industry" else 2, .8, ["Оформление: Договор ГПХ"]))
    result = await evaluate(
        JobPosting(
            source="hh", url="https://hh.ru/vacancy/8607", title="Technical Product Manager",
            description="Управление продуктом", hiring_format="Оформление: Договор ГПХ",
        ), {}, [{}], FakeGateway(complete), preference_policy=p,
    )
    assert result.decision == "skip"
    assert "preference_red_flag" in result.hard_rule_violations


@pytest.mark.asyncio
async def test_confirmed_negative_red_match_can_apply():
    complete = analysis(
        [FlagMatch(flag_id="red-1", matched=False, confidence=.8, evidence=[])],
        task_score=2,
    )
    complete.tasks.evidence = ["Разработка продукта"]
    for field in ("experience_depth", "role_match", "industry", "special_requirements"):
        setattr(complete, field, assessment(1 if field != "industry" else 2, .8, ["Разработка продукта"]))
    complete.skills = [SkillAssessment(skill="product", importance="required", score=1, evidence=["product"], explanation="x")]
    result = await evaluate(
        job(text="Разработка продукта"), {}, [{"skills": ["product"]}], FakeGateway(complete), preference_policy=policy(red=True),
    )
    assert result.decision == "apply"
    assert "preference_red_flag" not in result.hard_rule_violations


@pytest.mark.asyncio
async def test_missing_and_unknown_matches_are_handled_and_evidence_grounded():
    p = policy(red=True)
    gateway = FakeGateway(analysis([FlagMatch(flag_id="unknown", matched=True, confidence=1, evidence=["продажи"]), FlagMatch(flag_id="red-1", matched=True, confidence=1, evidence=["нет в вакансии"])]))
    result = await evaluate(job(text="Только разработка"), {}, [{}], gateway, preference_policy=p)
    assert [m.flag_id for m in result.flag_matches] == ["red-1"]
    assert result.flag_matches[0].matched is False
    assert "preference_red_flag" not in result.hard_rule_violations


@pytest.mark.asyncio
@pytest.mark.parametrize("offered, blocked", [(76_000, False), (75_000, True)])
async def test_salary_boundary(offered, blocked):
    p = policy(salary=True)
    gateway = FakeGateway(analysis())
    result = await evaluate(job(Salary(minimum=offered, maximum=offered)), {}, [{}], gateway, preference_policy=p)
    assert ("salary_below_preference" in result.hard_rule_violations) is blocked


@pytest.mark.asyncio
async def test_task_green_adds_one_once_and_is_capped():
    p = policy(task=True)
    matches = [FlagMatch(flag_id="green-task", matched=True, confidence=.7, evidence=["автоматизация задач"])]
    result = await evaluate(job(), {}, [{}], FakeGateway(analysis(matches, task_score=3)), preference_policy=p)
    row = next(item for item in result.score_breakdown if item.key == "tasks")
    assert row.raw_points == 4


@pytest.mark.asyncio
async def test_industry_query_can_come_without_resume():
    p = policy(industry=True)
    gateway = FakeGateway(analysis())
    queries = await plan_search_queries(gateway, [{"desired_title": "Редактор"}], preference_policy=p)
    assert queries == ["GameDev"]
    assert gateway.payloads[0][1]["preference_policy"]["green_flags"][0]["category"] == "desired_industry"


def test_role_prompts_are_policy_specific_and_hidden():
    payload = {"preference_policy": policy(industry=True).model_dump(mode="json")}
    assert "FlagMatch" in _system_prompt_for_role("resume_analyst", payload)
    assert "red никогда" in _system_prompt_for_role("search_planner", payload)
    assert "не должна раскрываться" in _system_prompt_for_role("writer", payload)
    assert "приоритетом" in _system_prompt_for_role("hirehi_category", payload)
    assert "не показывай" in _system_prompt_for_role("job_summary", payload)
