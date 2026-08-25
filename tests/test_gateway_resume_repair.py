import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from backend.intelligence import gateway as gateway_module
from backend.intelligence.gateway import (
    ModelGateway,
    ModelUnavailable,
    _schema_for_role,
)
from backend.intelligence.prompts import ROLE_PROMPTS
from backend.schemas.domain import ResumeAnalysis

CRITERIA = {
    "tasks",
    "skills",
    "experience_depth",
    "role_match",
    "industry",
    "special_requirements",
}


def _response(payload: dict) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(payload)))]
    )


def _valid_analysis() -> dict:
    assessment = {"score": 0, "confidence": 0, "explanation": "нет", "evidence": []}
    result = {name: dict(assessment) for name in CRITERIA - {"skills"}}
    result["skills"] = []
    return result


class _FakeCompletions:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return next(self.responses)


class _FakeClient:
    def __init__(self, completions):
        self.chat = SimpleNamespace(completions=completions)


def test_resume_analyst_prompt_requires_unwrapped_root_object():
    prompt = ROLE_PROMPTS["resume_analyst"]
    assert "ровно один JSON-объект ResumeAnalysis в корне" in prompt
    assert "analysis, resumes, candidate_name, result или data" in prompt
    assert "без total score" in prompt


def test_resume_analysis_wrapper_and_old_contract_are_rejected():
    with pytest.raises(ValidationError):
        ResumeAnalysis.model_validate_json('{"analysis": {}}')
    with pytest.raises(ValidationError):
        ResumeAnalysis.model_validate({
            "title": {}, "tasks": {}, "industry": {},
            "required_years": {}, "languages": {}, "skills": {},
        })


def test_resume_analysis_json_schema_requires_new_contract():
    schema = _schema_for_role("resume_analyst", ResumeAnalysis)
    assert set(schema["required"]) == CRITERIA
    assert set(schema["$defs"]["MatchAssessment"]["required"]) == {
        "score", "confidence", "evidence", "explanation",
    }
    assert set(schema["$defs"]["SkillAssessment"]["required"]) == {
        "skill", "importance", "score", "evidence", "explanation",
    }
    assert "score" not in schema["properties"]


@pytest.mark.asyncio
async def test_resume_analyst_repairs_wrappers_and_returns_valid_analysis(monkeypatch):
    completions = _FakeCompletions(
        [_response({key: {}}) for key in ("resumes", "analysis", "candidate_name")]
        + [_response(_valid_analysis())]
    )
    monkeypatch.setattr(gateway_module, "AsyncOpenAI", lambda **_: _FakeClient(completions))
    saved = SimpleNamespace(
        base_url="https://api.example.test/v1",
        model="test-model",
        encrypted_api_key="ciphertext",
    )
    monkeypatch.setattr(ModelGateway, "_saved_config", staticmethod(lambda: saved))
    monkeypatch.setattr(gateway_module, "decrypt_secret", lambda _: "test-key")

    result = await ModelGateway(provider="openai_compat").structured(
        "resume_analyst", {"job": {}, "resumes": []}, ResumeAnalysis
    )

    assert isinstance(result, ResumeAnalysis)
    assert len(completions.calls) == 4
    repair_message = completions.calls[1]["messages"][0]["content"]
    assert "локальную проверку" in repair_message
    assert "experience_depth" in repair_message
    assert "skills обязан быть массивом" in repair_message
    assert "required_years" not in repair_message
    assert "analysis, resumes, candidate_name" in repair_message


@pytest.mark.asyncio
async def test_resume_analyst_four_invalid_responses_raise_model_unavailable(monkeypatch):
    completions = _FakeCompletions([_response({"analysis": {}})] * 4)
    monkeypatch.setattr(gateway_module, "AsyncOpenAI", lambda **_: _FakeClient(completions))
    saved = SimpleNamespace(
        base_url="https://api.example.test/v1",
        model="test-model",
        encrypted_api_key="ciphertext",
    )
    monkeypatch.setattr(ModelGateway, "_saved_config", staticmethod(lambda: saved))
    monkeypatch.setattr(gateway_module, "decrypt_secret", lambda _: "test-key")

    with pytest.raises(ModelUnavailable, match="некорректный JSON"):
        await ModelGateway(provider="openai_compat").structured(
            "resume_analyst", {"job": {}, "resumes": []}, ResumeAnalysis
        )
    assert len(completions.calls) == 4
