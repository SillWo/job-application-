from __future__ import annotations

import pytest

from backend.intelligence.evaluator import evaluate
from backend.schemas.domain import (
    DesiredJobPolicy,
    FlagMatch,
    JobPosting,
    MatchAssessment,
    PreferenceFlag,
    ResumeAnalysis,
    SkillAssessment,
)


class _Gateway:
    def __init__(self, analysis):
        self.analysis = analysis

    async def structured(self, _role, _payload, _schema):
        return self.analysis


def _job():
    return JobPosting(
        source="hh",
        external_id="synthetic-1",
        url="https://hh.ru/vacancy/1",
        title="Game Developer",
        company="Example Studio",
        description=(
            "GameDev studio builds games. Implement game features and work with SQL."
        ),
    )


def _analysis(*, industry_score=0, industry_evidence=None, matches=()):
    return ResumeAnalysis(
        tasks=MatchAssessment(
            score=2, confidence=0.8, evidence=["Implement game features"]
        ),
        skills=[
            SkillAssessment(
                skill="SQL",
                importance="required",
                score=1,
                evidence=["SQL"],
                explanation="In resume",
            )
        ],
        experience_depth=MatchAssessment(
            score=1, confidence=0.8, evidence=["work with SQL"]
        ),
        role_match=MatchAssessment(
            score=1, confidence=0.8, evidence=["Game Developer"]
        ),
        industry=MatchAssessment(
            score=industry_score,
            confidence=0.8 if industry_score else 0,
            evidence=list(industry_evidence or []),
            explanation="Resume industry mismatch",
        ),
        special_requirements=MatchAssessment(),
        flag_matches=list(matches),
    )


def _industry_policy(*, source_quote="GameDev", version=2, required=False, red=False):
    return DesiredJobPolicy(
        contract_version=version,
        green_flags=[
            PreferenceFlag(
                id="green-industry",
                text="GameDev",
                category="desired_industry",
                required=required,
                source_quote=source_quote,
            )
        ],
        red_flags=(
            [PreferenceFlag(id="red-sales", text="Sales", category="other")]
            if red
            else []
        ),
    )


def _industry_match(*, matched=True, confidence=0.9, evidence="GameDev"):
    return FlagMatch(
        flag_id="green-industry",
        matched=matched,
        confidence=confidence,
        evidence=[evidence],
    )


@pytest.mark.asyncio
async def test_verified_new_industry_preference_overrides_resume_industry_mismatch():
    result = await evaluate(
        _job(),
        {},
        [{"skills": ["SQL"], "experience": "EdTech"}],
        _Gateway(_analysis(matches=[_industry_match()])),
        preference_policy=_industry_policy(),
    )

    industry = next(row for row in result.score_breakdown if row.key == "industry")
    assert result.decision == "apply"
    assert industry.raw_points == 4
    assert industry.evidence == ["GameDev"]
    assert "пожеланию по сфере" in industry.explanation.casefold()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("policy", "match"),
    [
        (None, None),
        (_industry_policy(), _industry_match(matched=False)),
        (_industry_policy(), _industry_match(confidence=0.69)),
        (_industry_policy(), _industry_match(evidence="не упомянуто")),
        (_industry_policy(source_quote=""), _industry_match()),
        (_industry_policy(version=1), _industry_match()),
    ],
)
async def test_unverified_or_legacy_industry_preference_cannot_override(
    policy, match
):
    matches = [match] if match else []
    result = await evaluate(
        _job(), {}, [{"skills": ["SQL"]}],
        _Gateway(_analysis(matches=matches)),
        preference_policy=policy,
    )

    industry = next(row for row in result.score_breakdown if row.key == "industry")
    assert result.decision == "skip"
    assert industry.raw_points == 0
    assert any(item.startswith("minimum_score:industry:") for item in result.hard_rule_violations)


@pytest.mark.asyncio
async def test_exact_preference_match_repairs_existing_positive_industry_without_evidence():
    result = await evaluate(
        _job(), {}, [{"skills": ["SQL"]}],
        _Gateway(
            _analysis(industry_score=1, industry_evidence=[], matches=[_industry_match()])
        ),
        preference_policy=_industry_policy(),
    )

    industry = next(row for row in result.score_breakdown if row.key == "industry")
    assert result.decision == "apply"
    assert industry.raw_points == 4
    assert industry.evidence == ["GameDev"]
    assert "industry" not in result.hard_rule_violations


@pytest.mark.asyncio
async def test_industry_override_does_not_bypass_red_or_required_green_gates():
    red_match = FlagMatch(
        flag_id="red-sales", matched=True, confidence=0.9, evidence=["Sales"]
    )
    red_job = _job().model_copy(
        update={"description": "GameDev studio builds games. Sales team manages SQL."}
    )
    red_result = await evaluate(
        red_job,
        {},
        [{"skills": ["SQL"]}],
        _Gateway(_analysis(matches=[_industry_match(), red_match])),
        preference_policy=_industry_policy(red=True),
    )
    assert red_result.decision == "skip"
    assert "preference_red_flag" in red_result.hard_rule_violations

    required_policy = _industry_policy()
    required_policy.green_flags.append(
        PreferenceFlag(
            id="green-required-role",
            text="Senior Developer",
            category="desired_task",
            required=True,
            source_quote="Senior Developer",
        )
    )
    required_result = await evaluate(
        _job(),
        {},
        [{"skills": ["SQL"]}],
        _Gateway(_analysis(matches=[_industry_match()])),
        preference_policy=required_policy,
    )
    assert required_result.decision == "skip"
    assert "preference_required_unconfirmed:green-required-role" in required_result.hard_rule_violations
