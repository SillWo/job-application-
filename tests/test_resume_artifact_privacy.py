from __future__ import annotations

import pytest

from backend.intelligence.letter_writer import write_cover_letter
from backend.orchestrator.workflow import (
    _cache_matches_resume,
    _redact_plan,
    _redact_private_string,
)
from backend.schemas.domain import ApplicationPlan, JobPosting

PRIVATE = {
    "identity": {
        "full_name": {"value": "Ada Lovelace", "availability": "present"},
    },
    "contacts": {
        "email": {"value": "ada@example.test", "availability": "present"},
        "phone": {"value": "+7 900 000-00-00", "availability": "present"},
        "messengers": {"value": ["https://t.me/ada"], "availability": "present"},
    },
}


def test_snapshot_artifact_cache_requires_exact_hash_and_keeps_legacy_compatibility():
    assert _cache_matches_resume({"_resume_content_hash": "a"}, "a")
    assert not _cache_matches_resume({"_resume_content_hash": "a"}, "b")
    assert not _cache_matches_resume({}, "a")
    assert _cache_matches_resume({}, None)


def test_redacted_plan_contains_placeholders_but_no_private_values():
    plan = ApplicationPlan(
        vacancy_id=1,
        resume_file="",
        cover_letter="{{full_name}} {{email}}",
        known_answers={"name": "Ada Lovelace"},
    )
    cached = _redact_plan(plan, PRIVATE)
    serialized = str(cached)
    assert "Ada Lovelace" not in serialized
    assert "ada@example.test" not in serialized
    assert "{{full_name}}" in serialized


def test_snapshot_private_values_are_redacted_from_preferences_before_models():
    redacted = _redact_private_string(
        "Ищите для Ada Lovelace, email ada@example.test, телефон +7 900 000-00-00",
        PRIVATE,
    )
    assert "Ada Lovelace" not in redacted
    assert "ada@example.test" not in redacted
    assert "+7 900 000-00-00" not in redacted
    assert "{{full_name}}" in redacted and "{{email}}" in redacted and "{{phone}}" in redacted


@pytest.mark.asyncio
async def test_snapshot_writer_never_sends_or_persists_private_values():
    calls = []

    class Gateway:
        async def structured(self, role, payload, schema):
            calls.append((role, payload))
            if role == "special_conditions":
                return schema.model_validate({"conditions": []})
            return schema.model_validate({
                "text": "Я Ada Lovelace, email ada@example.test, телефон +7 900 000-00-00. "
                "Готова обсудить задачи вакансии.",
                "fulfilled_special_conditions": [],
            })

    result = await write_cover_letter(
        JobPosting(source="test", url="https://example.test/job", title="Engineer", description="Задачи"),
        {"gender": "female"},
        [{"skills": ["Python"]}],
        Gateway(),
        cover_letter_auto=False,
        cover_letter_template="Ada Lovelace ada@example.test +7 900 000-00-00",
        private_view=PRIVATE,
    )
    payload_text = str(calls)
    assert "Ada Lovelace" not in payload_text
    assert "ada@example.test" not in payload_text
    assert "+7 900 000-00-00" not in payload_text
    assert "{{full_name}}" in result and "{{email}}" in result and "{{phone}}" in result
