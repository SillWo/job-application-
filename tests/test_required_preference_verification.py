from __future__ import annotations

from typing import Any

import pytest

from backend.intelligence.broker_gateway import BROKER_PROMPT_VERSION
from backend.intelligence.evaluator import evaluate
from backend.intelligence.gateway import ModelGateway, ModelPermanentError, ModelTimeout
from backend.intelligence.preference_policy import POLICY_CONTRACT_VERSION
from backend.orchestrator.workflow import _EVALUATION_CONTRACT
from backend.schemas.domain import (
    DesiredJobPolicy,
    FlagMatch,
    JobPosting,
    MatchAssessment,
    PreferenceFlag,
    RequiredPreferenceVerification,
    ResumeAnalysis,
    SkillAssessment,
)

ROLE_FLAG = PreferenceFlag(
    id="role",
    text="Product management or product analytics in an IT product",
    category="desired_industry",
    required=True,
    source_quote="product management or product analytics in an IT product",
)
TASK_FLAG = PreferenceFlag(
    id="tasks",
    text="Product research, hypotheses, and product measures",
    category="desired_task",
    required=True,
    source_quote="research, hypotheses, and product measures",
)


def _policy(*flags: PreferenceFlag, legacy: bool = False) -> DesiredJobPolicy:
    return DesiredJobPolicy(
        contract_version=1 if legacy else POLICY_CONTRACT_VERSION,
        green_flags=list(flags) if flags else [ROLE_FLAG, TASK_FLAG],
    )


def _job(title: str, description: str) -> JobPosting:
    return JobPosting(
        source="fixture",
        url="https://example.test/vacancy/1",
        title=title,
        company="Example",
        description=description,
        required_skills=["SQL"],
    )


def _analysis(flags: list[FlagMatch], job: JobPosting) -> ResumeAnalysis:
    return ResumeAnalysis(
        tasks=MatchAssessment(score=3, confidence=0.9, evidence=[job.description], explanation="Tasks."),
        skills=[SkillAssessment(skill="SQL", importance="required", score=2, evidence=["SQL"], explanation="Skill.")],
        experience_depth=MatchAssessment(score=3, confidence=0.9, evidence=[job.title], explanation="Experience."),
        role_match=MatchAssessment(score=3, confidence=0.9, evidence=[job.title], explanation="Role."),
        industry=MatchAssessment(score=3, confidence=0.9, evidence=[job.title], explanation="Industry."),
        special_requirements=MatchAssessment(score=1, confidence=0.9, evidence=[], explanation="No special requirements."),
        category="Product role",
        reason="Relevant role and tasks.",
        flag_matches=flags,
    )


def _primary_matches(job: JobPosting, flags: list[PreferenceFlag] | None = None) -> list[FlagMatch]:
    flags = flags or [ROLE_FLAG, TASK_FLAG]
    evidence = {
        "role": [job.title],
        "tasks": [job.description.split(".")[0]],
    }
    return [
        FlagMatch(flag_id=flag.id, matched=True, confidence=0.95, evidence=evidence.get(flag.id, [job.description]))
        for flag in flags
    ]


def _verify(
    flags: list[PreferenceFlag],
    *,
    matched: bool = True,
    evidence: dict[str, list[str]] | None = None,
    missing: dict[str, list[str]] | None = None,
    confidence: float = 0.97,
) -> dict[str, Any]:
    evidence = evidence or {flag.id: ["supported vacancy duty"] for flag in flags}
    missing = missing or {}
    return {
        "flags": [{
            "flag_id": flag.id,
            "matched": matched,
            "confidence": confidence,
            "evidence": evidence.get(flag.id, []),
            "missing_conditions": missing.get(flag.id, []),
            "explanation": "Проверены обязанности вакансии.",
        } for flag in flags],
    }


class EvaluatorGateway:
    def __init__(self, analysis: ResumeAnalysis, verification: Any = None):
        self.analysis = analysis
        self.verification = verification
        self.calls: list[tuple[str, dict, type]] = []

    async def structured(self, role: str, payload: dict, schema: type):
        self.calls.append((role, payload, schema))
        if role == "resume_analyst":
            return self.analysis
        if role == "required_preference_check":
            if isinstance(self.verification, Exception):
                raise self.verification
            if self.verification is None:
                raise AssertionError("Test must specify the independent verification result")
            return schema.model_validate(self.verification)
        raise AssertionError(f"Unexpected model role: {role}")


def _checker_call(gateway: EvaluatorGateway):
    return next(call for call in gateway.calls if call[0] == "required_preference_check")


@pytest.mark.asyncio
async def test_public_product_marketing_case_is_rejected_when_required_product_duties_are_missing():
    job = _job(
        "Product Marketing Manager",
        "Develop positioning, manage content and launch campaigns. Research and test marketing channels.",
    )
    flags = [ROLE_FLAG, TASK_FLAG]
    verification = _verify(
        flags,
        matched=False,
        evidence={
            "role": ["Product Marketing Manager"],
            "tasks": ["Research and test marketing channels"],
        },
        missing={
            "role": ["product management or product analytics duties"],
            "tasks": ["product measures"],
        },
    )
    gateway = EvaluatorGateway(_analysis(_primary_matches(job), job), verification)

    result = await evaluate(job, {"private": "must-not-be-sent"}, [{"resume": "private"}], gateway, preference_policy=_policy())

    assert result.decision == "skip"
    assert "preference_required_unconfirmed:role" in result.hard_rule_violations
    assert "preference_required_unconfirmed:tasks" in result.hard_rule_violations
    assert not next(match for match in result.flag_matches if match.flag_id == "role").matched
    role, payload, schema = _checker_call(gateway)
    assert role == "required_preference_check"
    assert set(payload) == {"job", "required_flags"}
    assert payload["job"] == job.model_dump(mode="json")
    assert payload["required_flags"] == [flag.model_dump(mode="json") for flag in flags]
    assert "profile" not in payload and "resumes" not in payload and "analysis" not in payload
    assert schema is RequiredPreferenceVerification


@pytest.mark.asyncio
async def test_research_and_hypotheses_without_product_measures_rejects_compound_required_flag():
    job = _job(
        "Business Analyst",
        "Research customer needs and formulate hypotheses. Analyze the results of business initiatives.",
    )
    flags = [TASK_FLAG]
    verification = _verify(
        flags,
        matched=False,
        evidence={"tasks": ["Research customer needs and formulate hypotheses"]},
        missing={"tasks": ["product measures or equivalent measured product outcomes"]},
    )
    gateway = EvaluatorGateway(_analysis(_primary_matches(job, flags), job), verification)

    result = await evaluate(job, {}, [{"skills": ["SQL"]}], gateway, preference_policy=_policy(TASK_FLAG))

    assert result.decision == "skip"
    assert "preference_required_unconfirmed:tasks" in result.hard_rule_violations


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("title", "description", "role_quote", "task_quote"),
    [
        (
            "Product Manager, DSSL",
            "Manage an IT product roadmap; interview users, track feature usage, test hypotheses and measure retention.",
            "Manage an IT product roadmap",
            "interview users, track feature usage, test hypotheses and measure retention",
        ),
        (
            "AI Product Owner",
            "Own an AI product; research user problems, prioritize hypotheses, run experiments and measure product outcomes.",
            "Own an AI product",
            "research user problems, prioritize hypotheses, run experiments and measure product outcomes",
        ),
        (
            "CRO Analyst",
            "Analyze product funnels and retention cohorts, investigate user behavior, and test experiments against product outcomes.",
            "Analyze product funnels and retention cohorts",
            "investigate user behavior, and test experiments against product outcomes",
        ),
    ],
)
@pytest.mark.asyncio
async def test_product_duties_and_semantic_equivalent_measures_pass_across_titles(
    title, description, role_quote, task_quote
):
    job = _job(title, description)
    flags = [ROLE_FLAG, TASK_FLAG]
    verification = _verify(flags, evidence={"role": [role_quote], "tasks": [task_quote]})
    gateway = EvaluatorGateway(_analysis(_primary_matches(job), job), verification)

    result = await evaluate(job, {}, [{"skills": ["SQL"]}], gateway, preference_policy=_policy())

    assert result.decision == "apply", (result.reason, result.hard_rule_violations)
    assert all(match.matched for match in result.flag_matches if match.flag_id in {"role", "tasks"})


@pytest.mark.asyncio
async def test_explicit_marketing_preference_can_match_marketing_duties():
    marketing = PreferenceFlag(
        id="marketing",
        text="Product marketing, positioning and go-to-market",
        category="desired_task",
        required=True,
        source_quote="Product marketing, positioning and go-to-market",
    )
    job = _job("Product Marketing Manager", "Own product positioning, go-to-market planning and campaign messaging.")
    gateway = EvaluatorGateway(
        _analysis(_primary_matches(job, [marketing]), job),
        _verify([marketing], evidence={"marketing": [job.description]}),
    )

    result = await evaluate(job, {}, [{"skills": ["SQL"]}], gateway, preference_policy=_policy(marketing))

    assert result.decision == "apply", (result.reason, result.hard_rule_violations)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "verification",
    [
        {"flags": []},
        {"flags": [
            {"flag_id": "role", "matched": True, "confidence": 1, "evidence": ["Product Analyst"], "missing_conditions": [], "explanation": "ok"},
            {"flag_id": "role", "matched": True, "confidence": 1, "evidence": ["Product Analyst"], "missing_conditions": [], "explanation": "duplicate"},
            {"flag_id": "tasks", "matched": True, "confidence": 1, "evidence": ["product research and анализ продуктовых метрик"], "missing_conditions": [], "explanation": "ok"},
        ]},
        {"flags": [
            {"flag_id": "role", "matched": True, "confidence": 1, "evidence": ["Product Analyst"], "missing_conditions": [], "explanation": "ok"},
            {"flag_id": "tasks", "matched": True, "confidence": 1, "evidence": ["product research and анализ продуктовых метрик"], "missing_conditions": [], "explanation": "ok"},
            {"flag_id": "extra", "matched": True, "confidence": 1, "evidence": ["Product Analyst"], "missing_conditions": [], "explanation": "extra"},
        ]},
        {"flags": [
            {"flag_id": "role", "matched": True, "confidence": 0.89, "evidence": ["Product Analyst"], "missing_conditions": [], "explanation": "low confidence"},
            {"flag_id": "tasks", "matched": True, "confidence": 0.97, "evidence": ["product research and анализ продуктовых метрик"], "missing_conditions": [], "explanation": "ok"},
        ]},
        {"flags": [
            {"flag_id": "role", "matched": True, "confidence": 0.97, "evidence": ["invented quote"], "missing_conditions": [], "explanation": "ungrounded"},
            {"flag_id": "tasks", "matched": True, "confidence": 0.97, "evidence": ["product research and анализ продуктовых метрик"], "missing_conditions": [], "explanation": "ok"},
        ]},
        {"flags": [
            {"flag_id": "role", "matched": True, "confidence": 0.97, "evidence": ["Product Analyst"], "missing_conditions": ["IT product"], "explanation": "inconsistent"},
            {"flag_id": "tasks", "matched": True, "confidence": 0.97, "evidence": ["product research and анализ продуктовых метрик"], "missing_conditions": [], "explanation": "ok"},
        ]},
    ],
)
@pytest.mark.asyncio
async def test_malformed_or_weak_verifier_output_cannot_admit(verification):
    job = _job("Product Analyst", "IT role: product research and анализ продуктовых метрик.")
    gateway = EvaluatorGateway(_analysis(_primary_matches(job), job), verification)

    result = await evaluate(job, {}, [{"skills": ["SQL"]}], gateway, preference_policy=_policy())

    assert result.decision == "skip"
    assert any(item.startswith("preference_required_unconfirmed:") for item in result.hard_rule_violations)


@pytest.mark.asyncio
async def test_primary_required_gate_failure_does_not_call_independent_checker():
    job = _job("Product Analyst", "IT role: product research and анализ продуктовых метрик.")
    matches = _primary_matches(job)
    matches[0] = matches[0].model_copy(update={"matched": False})
    gateway = EvaluatorGateway(_analysis(matches, job))

    result = await evaluate(job, {}, [{"skills": ["SQL"]}], gateway, preference_policy=_policy())

    assert result.decision == "skip"
    assert [role for role, _payload, _schema in gateway.calls] == ["resume_analyst"]


@pytest.mark.asyncio
async def test_duplicate_required_policy_ids_fail_closed_without_checker_call():
    job = _job("Product Analyst", "IT role: product research and анализ продуктовых метрик.")
    duplicate = ROLE_FLAG.model_copy(update={"text": "Another required role interpretation"})
    policy = _policy(ROLE_FLAG, duplicate)
    gateway = EvaluatorGateway(_analysis(_primary_matches(job, [ROLE_FLAG]), job))

    result = await evaluate(job, {}, [{"skills": ["SQL"]}], gateway, preference_policy=policy)

    assert result.decision == "skip"
    assert "preference_required_unconfirmed:role" in result.hard_rule_violations
    assert not next(match for match in result.flag_matches if match.flag_id == "role").matched
    assert [role for role, _payload, _schema in gateway.calls] == ["resume_analyst"]


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True])
async def test_no_required_or_legacy_policy_does_not_call_checker(legacy):
    job = _job("Product Analyst", "IT role: product research and анализ продуктовых метрик.")
    flags = [] if legacy else [PreferenceFlag(
        id="optional", text="Optional product duties", category="desired_task", required=False,
    )]
    gateway = EvaluatorGateway(_analysis([], job))

    result = await evaluate(job, {}, [{"skills": ["SQL"]}], gateway, preference_policy=_policy(*flags, legacy=legacy))

    assert result.decision == "apply", (result.reason, result.hard_rule_violations)
    assert [role for role, _payload, _schema in gateway.calls] == ["resume_analyst"]


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [ModelTimeout("timeout"), ModelPermanentError("provider terminal")])
async def test_typed_verifier_provider_failures_propagate(error):
    job = _job("Product Analyst", "IT role: product research and анализ продуктовых метрик.")
    gateway = EvaluatorGateway(_analysis(_primary_matches(job), job), error)

    with pytest.raises(type(error)):
        await evaluate(job, {}, [{"skills": ["SQL"]}], gateway, preference_policy=_policy())


@pytest.mark.asyncio
async def test_mock_verifier_fails_closed_for_required_flags():
    result = await ModelGateway(provider="mock").structured(
        "required_preference_check",
        {"job": {}, "required_flags": [ROLE_FLAG.model_dump(mode="json")]},
        RequiredPreferenceVerification,
    )

    assert len(result.flags) == 1
    assert result.flags[0].flag_id == ROLE_FLAG.id
    assert result.flags[0].matched is False
    assert result.flags[0].evidence == []
    assert result.flags[0].missing_conditions


def test_required_verification_contract_versions_invalidate_old_analysis_cache():
    assert BROKER_PROMPT_VERSION != "workflow-prompts-2026-10-08-required-preferences-v1"
    assert _EVALUATION_CONTRACT != "job-evaluation-v3-industry-preference"
    assert "required-verification" in BROKER_PROMPT_VERSION
    assert "required-preference-verification" in _EVALUATION_CONTRACT
