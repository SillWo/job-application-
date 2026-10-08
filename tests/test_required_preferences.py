from __future__ import annotations

import pytest

from backend.intelligence.evaluator import evaluate
from backend.intelligence.gateway import ModelPermanentError
from backend.intelligence.preference_policy import (
    POLICY_CONTRACT_VERSION,
    _normalize,
    compile_preference_policy,
)
from backend.schemas.domain import (
    DesiredJobPolicy,
    FlagMatch,
    JobPosting,
    MatchAssessment,
    PreferenceFlag,
    ResumeAnalysis,
    SalaryPreference,
    SkillAssessment,
)

DESCRIPTION = (
    "Ищу Product Manager или Product Analyst в IT. Важны product research "
    "и анализ продуктовых метрик."
)
ROLE_QUOTE = "Product Manager или Product Analyst в IT"
TASK_QUOTE = "product research и анализ продуктовых метрик"


class _CompilerGateway:
    def __init__(self, result):
        self.result = result

    async def structured(self, role, payload, schema):
        assert role == "preference_compiler"
        assert schema is DesiredJobPolicy
        return self.result


class _EvaluatorGateway:
    def __init__(self, matches, verification=None):
        self.matches = matches
        self.verification = verification

    async def structured(self, role, payload, schema):
        if role == "required_preference_check":
            # Existing evaluator tests opt in to an explicit synthetic proof;
            # absent proof remains fail-closed.
            return schema.model_validate(self.verification or {"flags": []})
        assert role == "resume_analyst"
        return ResumeAnalysis(
            tasks=MatchAssessment(
                score=2,
                confidence=0.8,
                evidence=["анализ продуктовых метрик"],
                explanation="Совпадают задачи.",
            ),
            skills=[
                SkillAssessment(
                    skill="SQL",
                    importance="required",
                    score=1,
                    evidence=["SQL"],
                    explanation="Есть в резюме.",
                )
            ],
            experience_depth=MatchAssessment(
                score=2, confidence=0.8, evidence=["Product Analyst"]
            ),
            role_match=MatchAssessment(
                score=2, confidence=0.8, evidence=["Product Analyst"]
            ),
            industry=MatchAssessment(score=2, confidence=0.8, evidence=["Product Analyst"]),
            special_requirements=MatchAssessment(),
            reason="Роль и задачи близки.",
            flag_matches=self.matches,
        )


def _verification(flags, evidence_by_id):
    return {
        "flags": [
            {
                "flag_id": flag.id,
                "matched": True,
                "confidence": 0.97,
                "evidence": evidence_by_id[flag.id],
                "missing_conditions": [],
                "explanation": "Synthetic job evidence confirms the required conditions.",
            }
            for flag in flags
        ]
    }


def _required_policy(*, extra_flags=(), legacy=False) -> DesiredJobPolicy:
    return DesiredJobPolicy(
        contract_version=1 if legacy else POLICY_CONTRACT_VERSION,
        green_flags=[
            PreferenceFlag(
                id="green-1",
                text="Product Manager OR Product Analyst (IT)",
                category="desired_industry",
                required=True,
                source_quote=ROLE_QUOTE,
            ),
            PreferenceFlag(
                id="green-2",
                text="product research and product metrics",
                category="desired_task",
                required=True,
                source_quote=TASK_QUOTE,
            ),
            PreferenceFlag(
                id="green-3",
                text="B2B SaaS experience",
                category="desired_industry",
            ),
            *extra_flags,
        ],
    )


def _job(*, salary=None, hiring_format=None, title="Product Analyst"):
    return JobPosting(
        source="hh",
        external_id="12447",
        url="https://hh.ru/vacancy/12447",
        title=title,
        company="Example",
        description="IT role: product research and анализ продуктовых метрик.",
        salary=salary,
        hiring_format=hiring_format,
    )


def _matches(*, role=True, role_confidence=0.9, role_evidence=None, research=True):
    flags = [
        FlagMatch(
            flag_id="green-1",
            matched=role,
            confidence=role_confidence,
            evidence=role_evidence if role_evidence is not None else (["Product Analyst"] if role else []),
        ),
        FlagMatch(
            flag_id="green-2",
            matched=research,
            confidence=0.9,
            evidence=["анализ продуктовых метрик"] if research else [],
        ),
        FlagMatch(flag_id="green-3", matched=False, confidence=0.9, evidence=[]),
    ]
    return flags


@pytest.mark.asyncio
async def test_compile_outputs_v2_and_accepts_exact_user_quote():
    compiler_result = DesiredJobPolicy(
        green_flags=[
            PreferenceFlag(
                id="model-id",
                text="Product Manager OR Product Analyst",
                category="desired_industry",
                required=True,
                source_quote="product manager или product analyst в it",
            )
        ]
    )

    policy = await compile_preference_policy(_CompilerGateway(compiler_result), DESCRIPTION)

    assert policy.contract_version == POLICY_CONTRACT_VERSION == 2
    assert policy.green_flags[0].id == "green-1"
    assert policy.green_flags[0].required


@pytest.mark.asyncio
async def test_compile_preserves_required_contract_flag_with_exact_quote():
    description = "Ищу продуктового аналитика в IT с трудовым договором."
    compiler_result = DesiredJobPolicy(
        green_flags=[
            PreferenceFlag(
                text="Продуктовая аналитика IT-продукта с трудовым договором",
                category="desired_industry",
                required=True,
                source_quote="продуктового аналитика в IT с трудовым договором",
            )
        ]
    )

    policy = await compile_preference_policy(
        _CompilerGateway(compiler_result), description
    )

    assert policy.green_flags[0].required is True


@pytest.mark.asyncio
@pytest.mark.parametrize("quote", [None, "трудовой договор через ООО"])
async def test_compile_rejects_required_contract_without_exact_user_quote(quote):
    compiler_result = DesiredJobPolicy(
        green_flags=[
            PreferenceFlag(
                text="Продуктовая аналитика с трудовым договором",
                category="desired_industry",
                required=True,
                source_quote=quote,
            )
        ]
    )

    with pytest.raises(ModelPermanentError) as error:
        await compile_preference_policy(
            _CompilerGateway(compiler_result),
            "Ищу продуктового аналитика в IT с официальным оформлением.",
        )

    assert error.value.error_code == "policy_grounding_invalid"


@pytest.mark.asyncio
@pytest.mark.parametrize("quote", [None, "должен быть продуктовый директор"])
async def test_compile_rejects_missing_or_forged_required_quote(quote):
    compiler_result = DesiredJobPolicy(
        green_flags=[
            PreferenceFlag(
                text="Product manager",
                category="desired_industry",
                required=True,
                source_quote=quote,
            )
        ]
    )

    with pytest.raises(ModelPermanentError) as error:
        await compile_preference_policy(_CompilerGateway(compiler_result), DESCRIPTION)

    assert error.value.error_code == "policy_grounding_invalid"


def test_normalize_only_forces_salary_category_and_red_flags_optional():
    policy = _normalize(
        DesiredJobPolicy(
            green_flags=[
                PreferenceFlag(
                    text="Желаемый доход",
                    category="desired_salary",
                    required=True,
                ),
                PreferenceFlag(
                    text="Трудовой договор",
                    category="other",
                    required=True,
                    source_quote="трудовой договор",
                ),
            ],
            red_flags=[
                PreferenceFlag(
                    text="Не продажи",
                    category="other",
                    required=True,
                )
            ],
            desired_salary=SalaryPreference(minimum_monthly_amount=200_000),
        )
    )

    assert policy.contract_version == POLICY_CONTRACT_VERSION
    assert policy.green_flags[0].required is False
    assert policy.green_flags[1].required is True
    assert all(not flag.required for flag in policy.red_flags)


def test_legacy_policy_defaults_to_v1_and_optional_green():
    legacy = DesiredJobPolicy.model_validate(
        {"green_flags": [{"text": "B2B", "category": "desired_industry"}]}
    )

    assert legacy.contract_version == 1
    assert legacy.green_flags[0].required is False
    assert legacy.green_flags[0].source_quote is None


@pytest.mark.asyncio
async def test_unconfirmed_required_role_blocks_even_with_high_resume_overlap_and_task_match():
    result = await evaluate(
        _job(),
        {},
        [{"skills": ["SQL"]}],
        _EvaluatorGateway(_matches(role=False, research=True)),
        preference_policy=_required_policy(),
    )

    assert result.decision == "skip"
    assert "preference_required_unconfirmed:green-1" in result.hard_rule_violations
    assert "обязательное требование" in result.reason.casefold()


@pytest.mark.asyncio
async def test_required_role_mentioning_contract_still_blocks_when_role_is_unconfirmed():
    policy = _required_policy()
    policy.green_flags[0] = policy.green_flags[0].model_copy(
        update={
            "text": "Product Analyst в IT с трудовым договором",
            "source_quote": "Product Analyst в IT с трудовым договором",
        }
    )
    policy = _normalize(policy)
    result = await evaluate(
        _job(), {}, [{"skills": ["SQL"]}],
        _EvaluatorGateway(_matches(role=False, research=True)),
        preference_policy=policy,
    )

    assert policy.green_flags[0].required is True
    assert result.decision == "skip"
    assert "preference_required_unconfirmed:green-1" in result.hard_rule_violations


@pytest.mark.asyncio
async def test_grounded_role_and_required_task_apply_while_optional_flag_is_false():
    policy = _required_policy()
    result = await evaluate(
        _job(),
        {},
        [{"skills": ["SQL"]}],
        _EvaluatorGateway(
            _matches(),
            _verification(policy.green_flags[:2], {
                "green-1": ["Product Analyst"],
                "green-2": ["анализ продуктовых метрик"],
            }),
        ),
        preference_policy=policy,
    )

    assert result.decision == "apply"
    assert not any(item.startswith("preference_required_unconfirmed:") for item in result.hard_rule_violations)
    assert next(match for match in result.flag_matches if match.flag_id == "green-3").matched is False


@pytest.mark.asyncio
async def test_required_evidence_must_be_exact_quote_not_fuzzy_token_overlap():
    policy = _required_policy()
    policy.green_flags[0] = policy.green_flags[0].model_copy(
        update={"text": "product management", "source_quote": "product management"}
    )
    result = await evaluate(
        _job(title="Product Analyst, Project management"),
        {},
        [{"skills": ["SQL"]}],
        _EvaluatorGateway(
            _matches(role_evidence=["product management"])
        ),
        preference_policy=policy,
    )

    assert result.decision == "skip"
    assert "preference_required_unconfirmed:green-1" in result.hard_rule_violations


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("company", "evidence", "expected_decision"),
    [
        ("Ozon Офис и Коммерция", "Ozon Офис и Коммерция", "apply"),
        ("Другой работодатель", "Ozon Офис и Коммерция", "skip"),
    ],
)
async def test_required_company_evidence_must_match_actual_employer(
    company, evidence, expected_decision
):
    job = _job().model_copy(update={"company": company})
    policy = DesiredJobPolicy(
        contract_version=POLICY_CONTRACT_VERSION,
        green_flags=[
            PreferenceFlag(
                id="green-1",
                text="Работа в Ozon",
                category="desired_industry",
                required=True,
                source_quote="Ozon Офис и Коммерция",
            )
        ],
    )
    result = await evaluate(
        job,
        {},
        [{"skills": ["SQL"]}],
        _EvaluatorGateway(
            [
                FlagMatch(
                    flag_id="green-1",
                    matched=True,
                    confidence=0.95,
                    evidence=[evidence],
                )
            ],
            _verification(policy.green_flags, {"green-1": [evidence]}),
        ),
        preference_policy=policy,
    )

    assert result.decision == expected_decision
    if expected_decision == "skip":
        assert "preference_required_unconfirmed:green-1" in result.hard_rule_violations


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True])
async def test_missing_company_does_not_break_required_or_legacy_policy_evaluation(legacy):
    job = _job().model_copy(update={"company": None})
    policy = _required_policy(legacy=legacy)
    result = await evaluate(
        job,
        {},
        [{"skills": ["SQL"]}],
        _EvaluatorGateway(
            _matches(),
            _verification(policy.green_flags[:2], {
                "green-1": ["Product Analyst"],
                "green-2": ["анализ продуктовых метрик"],
            }),
        ),
        preference_policy=policy,
    )

    assert result.decision == "apply"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "match_kwargs",
    [
        {"role": False, "role_confidence": 0.9},
        {"role": True, "role_confidence": 0.69},
        {"role": True, "role_confidence": 0.9, "role_evidence": ["not in the vacancy"]},
        {"role": True, "role_confidence": 0.9, "role_evidence": []},
    ],
)
async def test_missing_low_confidence_or_ungrounded_required_match_fails_closed(match_kwargs):
    flags = _matches(**match_kwargs)
    if match_kwargs == {"role": False, "role_confidence": 0.9}:
        flags = [item for item in flags if item.flag_id != "green-1"]
    result = await evaluate(
        _job(), {}, [{"skills": ["SQL"]}], _EvaluatorGateway(flags),
        preference_policy=_required_policy(),
    )

    assert result.decision == "skip"
    assert "preference_required_unconfirmed:green-1" in result.hard_rule_violations


@pytest.mark.asyncio
async def test_one_grounded_alternative_in_single_or_flag_satisfies_required_role():
    policy = _required_policy()
    result = await evaluate(
        _job(title="Product Analyst"),
        {},
        [{"skills": ["SQL"]}],
        _EvaluatorGateway(
            _matches(role_evidence=["Product Analyst"]),
            _verification(policy.green_flags[:2], {
                "green-1": ["Product Analyst"],
                "green-2": ["анализ продуктовых метрик"],
            }),
        ),
        preference_policy=policy,
    )

    assert result.decision == "apply"


@pytest.mark.asyncio
async def test_unknown_salary_and_contract_remain_optional_when_role_is_confirmed():
    optional_unknowns = [
        PreferenceFlag(
            text="Желаемый доход от 200000 RUB",
            category="desired_salary",
            required=True,
            source_quote="доход от 200000 рублей",
        ),
        PreferenceFlag(
            text="Трудовой договор",
            category="other",
            required=False,
            source_quote="трудовой договор",
        ),
    ]
    policy = _normalize(_required_policy(extra_flags=optional_unknowns))
    required_flags = [flag for flag in policy.green_flags if flag.required]
    result = await evaluate(
        _job(salary=None, hiring_format=None),
        {},
        [{"skills": ["SQL"]}],
        _EvaluatorGateway(
            _matches(),
            _verification(required_flags, {
                "green-1": ["Product Analyst"],
                "green-2": ["анализ продуктовых метрик"],
            }),
        ),
        preference_policy=policy,
    )

    assert all(not flag.required for flag in policy.green_flags[-2:])
    assert result.decision == "apply"
