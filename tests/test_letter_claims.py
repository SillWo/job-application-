from __future__ import annotations

import pytest

from backend.intelligence.gateway import ModelTimeout
from backend.intelligence.letter_claims import (
    LETTER_CLAIMS_VERSION,
    CandidateClaimCheck,
    CandidateClaimValidationError,
    validate_candidate_claims,
)
from backend.intelligence.letter_writer import (
    CoverLetterValidationError,
    validate_existing_letter_claims,
    write_cover_letter,
)
from backend.schemas.domain import JobPosting

RESUME = {"about": "Формировала технические задания и backlog."}
CLAIM = "Я умею формировать технические задания и backlog."


class CheckerGateway:
    def __init__(self, response: dict):
        self.response = response
        self.calls = []

    async def structured(self, role, payload, schema):
        self.calls.append((role, payload, schema))
        return schema.model_validate(self.response)


def _supported(claim: str = CLAIM, *, resume_index: int = 0, quote: str = "технические задания и backlog"):
    return {
        "all_candidate_claims_supported": True,
        "confidence": 0.97,
        "unsupported_claims": [],
        "evidence": [{"claim_span": claim, "resume_index": resume_index, "source_quote": quote}],
    }


@pytest.mark.asyncio
async def test_claim_check_sends_only_letter_and_professional_resumes_and_checks_exact_evidence():
    gateway = CheckerGateway(_supported())

    await validate_candidate_claims(gateway, CLAIM, [RESUME])

    assert LETTER_CLAIMS_VERSION == "candidate-claims-v1"
    role, payload, schema = gateway.calls[0]
    assert role == "letter_claim_check"
    assert set(payload) == {"letter", "resumes"}
    assert payload["letter"] == CLAIM
    assert schema is CandidateClaimCheck


@pytest.mark.parametrize("response", [
    _supported(quote="User Stories"),
    _supported(resume_index=1),
    _supported(claim="Я владею SQL."),
    {
        "all_candidate_claims_supported": True,
        "confidence": 0.89,
        "unsupported_claims": [],
        "evidence": [{"claim_span": CLAIM, "resume_index": 0, "source_quote": "backlog"}],
    },
    {
        "all_candidate_claims_supported": False,
        "confidence": 0.99,
        "unsupported_claims": ["не подтверждено"],
        "evidence": [],
    },
])
@pytest.mark.asyncio
async def test_claim_check_rejects_fabricated_quotes_indices_spans_and_low_confidence(response):
    with pytest.raises(CandidateClaimValidationError):
        await validate_candidate_claims(CheckerGateway(response), CLAIM, [RESUME])


@pytest.mark.asyncio
async def test_claim_check_rejects_json_key_as_evidence_and_missing_evidence_for_personal_fact():
    key_only = CheckerGateway(_supported(quote="about"))
    with pytest.raises(CandidateClaimValidationError):
        await validate_candidate_claims(key_only, CLAIM, [RESUME])

    missing = CheckerGateway({
        "all_candidate_claims_supported": True,
        "confidence": 1,
        "unsupported_claims": [],
        "evidence": [],
    })
    with pytest.raises(CandidateClaimValidationError):
        await validate_candidate_claims(missing, CLAIM, [RESUME])


@pytest.mark.parametrize("letter", [
    "Я привлёк 2,5 млн рублей за квартал.",
    "Образование: высшее, прикладная математика.",
    "У меня 10 лет опыта работы.",
])
@pytest.mark.asyncio
async def test_default_achievement_education_and_years_patterns_require_resume_evidence(letter):
    empty_evidence = CheckerGateway({
        "all_candidate_claims_supported": True,
        "confidence": 0.99,
        "unsupported_claims": [],
        "evidence": [],
    })

    with pytest.raises(CandidateClaimValidationError):
        await validate_candidate_claims(empty_evidence, letter, [RESUME])


@pytest.mark.asyncio
async def test_each_separate_candidate_fact_requires_its_own_evidence():
    resume = {
        "achievement": "Привлёк 2,5 млн рублей за квартал.",
        "about": "Формировала технические задания и backlog.",
    }
    first = "Я привлёк 2,5 млн рублей за квартал."
    second = "Я умею формировать технические задания и backlog."
    only_one_evidence = CheckerGateway({
        "all_candidate_claims_supported": True,
        "confidence": 0.99,
        "unsupported_claims": [],
        "evidence": [{
            "claim_span": first,
            "resume_index": 0,
            "source_quote": "Привлёк 2,5 млн рублей за квартал.",
        }],
    })

    with pytest.raises(CandidateClaimValidationError):
        await validate_candidate_claims(only_one_evidence, first + "\n" + second, [resume])


@pytest.mark.asyncio
async def test_metadata_values_cannot_be_used_as_evidence_quotes():
    resume = {
        "source_site": "hh",
        "source_section": "skills",
        "metadata": {"availability": "present", "schema_version": "resume-v2"},
        "skills": [],
    }
    with pytest.raises(CandidateClaimValidationError):
        await validate_candidate_claims(CheckerGateway(_supported(quote="hh")), "Я владею hh.", [resume])


@pytest.mark.asyncio
async def test_aspiration_company_interest_and_employer_knowledge_answer_need_no_candidate_evidence():
    gateway = CheckerGateway({
        "all_candidate_claims_supported": True,
        "confidence": 0.99,
        "unsupported_claims": [],
        "evidence": [],
    })
    letter = "Мне интересна компания «Тест». Буду рад обсудить роль. Лондон — столица Великобритании."

    await validate_candidate_claims(gateway, letter, [RESUME])


def _job() -> JobPosting:
    return JobPosting(source="test", url="https://example.test", title="Аналитик", company="Тест", description="Нужны User Stories.")


class WriterBoundaryGateway:
    def __init__(self, drafts: list[str], checker_responses: list[dict]):
        self.drafts = drafts
        self.checker_responses = checker_responses
        self.writer_calls = 0
        self.check_calls = 0
        self.calls = []

    async def structured(self, role, payload, schema):
        self.calls.append((role, payload, schema))
        if role == "special_conditions":
            return schema.model_validate({"conditions": []})
        if role == "writer":
            text = self.drafts[self.writer_calls]
            self.writer_calls += 1
            return schema.model_validate({"text": text, "fulfilled_special_conditions": []})
        if role == "letter_claim_check":
            response = self.checker_responses[self.check_calls]
            self.check_calls += 1
            return schema.model_validate(response)
        raise AssertionError(role)


@pytest.mark.asyncio
async def test_writer_rejects_vacancy_invented_skill_then_accepts_supported_rewrite():
    unsupported = "Я умею формировать User Stories."
    supported = "Я умею формировать технические задания и backlog."
    gateway = WriterBoundaryGateway(
        [unsupported, supported],
        [
            {
                "all_candidate_claims_supported": False,
                "confidence": 0.99,
                "unsupported_claims": [unsupported],
                "evidence": [],
            },
            _supported(supported),
        ],
    )

    result = await write_cover_letter(_job(), {"gender": "female"}, [RESUME], gateway)

    assert result == supported
    assert gateway.writer_calls == gateway.check_calls == 2
    checker_payload = next(payload for role, payload, _ in gateway.calls if role == "letter_claim_check")
    assert set(checker_payload) == {"letter", "resumes"}
    assert "User Stories" not in str(checker_payload["resumes"])


@pytest.mark.asyncio
async def test_writer_stops_after_three_unsupported_generations_without_echoing_draft():
    unsupported = "Я владею User Stories."
    gateway = WriterBoundaryGateway(
        [unsupported] * 3,
        [{
            "all_candidate_claims_supported": False,
            "confidence": 0.99,
            "unsupported_claims": ["model-controlled text must not be echoed"],
            "evidence": [],
        }] * 3,
    )

    with pytest.raises(CoverLetterValidationError, match="соответствующее требованиям"):
        await write_cover_letter(_job(), {"gender": "female"}, [RESUME], gateway)

    assert gateway.writer_calls == gateway.check_calls == 3
    writer_payloads = [payload for role, payload, _ in gateway.calls if role == "writer"]
    assert all("model-controlled text" not in str(payload) for payload in writer_payloads)


@pytest.mark.asyncio
async def test_provider_timeout_from_checker_propagates_and_a_later_call_can_recover():
    class RecoveringGateway(WriterBoundaryGateway):
        def __init__(self, drafts, checker_responses, *, fail_first=False):
            super().__init__(drafts, checker_responses)
            self.fail_first = fail_first

        async def structured(self, role, payload, schema):
            if self.fail_first and role == "letter_claim_check" and self.check_calls == 0:
                self.check_calls += 1
                raise ModelTimeout("timeout")
            return await super().structured(role, payload, schema)

    response = _supported()
    first = RecoveringGateway([CLAIM], [response], fail_first=True)
    with pytest.raises(ModelTimeout):
        await write_cover_letter(_job(), {"gender": "female"}, [RESUME], first)

    recovered = RecoveringGateway([CLAIM], [response])
    assert await write_cover_letter(_job(), {"gender": "female"}, [RESUME], recovered) == CLAIM


@pytest.mark.asyncio
async def test_cached_letter_wrapper_redacts_private_values_and_converts_only_claim_rejection():
    private = {"identity": {"full_name": "Иван Иванов"}, "contacts": {"phone": "+7 999 000-00-00"}}
    gateway = CheckerGateway(_supported())

    await validate_existing_letter_claims(
        gateway,
        "Иван Иванов, +7 999 000-00-00. " + CLAIM,
        [{**RESUME, "identity": {"full_name": "Иван Иванов"}}],
        private_view=private,
    )

    payload = gateway.calls[0][1]
    assert "Иван Иванов" not in payload["letter"]
    assert "+7 999 000-00-00" not in payload["letter"]
    assert "identity" not in payload["resumes"][0]

    rejected = CheckerGateway({
        "all_candidate_claims_supported": False,
        "confidence": 0.99,
        "unsupported_claims": ["do not expose this"],
        "evidence": [],
    })
    with pytest.raises(CoverLetterValidationError, match="неподтверждённые факты") as error:
        await validate_existing_letter_claims(rejected, CLAIM, [RESUME])
    assert "do not expose" not in str(error.value)
