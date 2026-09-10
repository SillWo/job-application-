"""Regression coverage for grounded special-condition cover-letter contracts."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from backend.intelligence.letter_writer import (
    CoverLetterGenerationDraft,
    CoverLetterValidationError,
    SpecialConditionBatch,
    write_cover_letter,
)
from backend.schemas.domain import JobPosting


def _job(description: str) -> JobPosting:
    return JobPosting(
        source="test",
        url="https://example.test/vacancy",
        title="Аналитик",
        company="Тест",
        description=description,
    )


class RecordingGateway:
    """Return model-validated fixtures while retaining every role and payload."""

    def __init__(self, conditions: list[dict[str, object]], text: str, fulfilled: list[dict[str, str]]):
        self.conditions = conditions
        self.text = text
        self.fulfilled = fulfilled
        self.calls: list[tuple[str, dict[str, object], type[object]]] = []

    async def structured(self, role: str, payload: dict[str, object], schema: type[object]):
        self.calls.append((role, payload, schema))
        if role == "special_conditions":
            return schema.model_validate({"conditions": self.conditions})  # type: ignore[attr-defined]
        return schema.model_validate({  # type: ignore[attr-defined]
            "text": self.text,
            "fulfilled_special_conditions": self.fulfilled,
        })


async def _write(
    gateway: RecordingGateway,
    description: str,
    *,
    auto: bool = True,
    template: str = "",
) -> str:
    return await write_cover_letter(
        _job(description),
        {"full_name": "Иван Иванов", "gender": "male"},
        [{"name": "Резюме", "skills": ["SQL"]}],
        gateway,
        cover_letter_auto=auto,
        cover_letter_template=template,
    )


@pytest.mark.asyncio
async def test_150_ordinary_and_170_semantic_special_words_are_preserved() -> None:
    source = "Работодатель просит в письме раскрыть этот смысловой блок."
    special = " ".join(f"смысловой{i}" for i in range(170))
    text = " ".join(["обычное"] * 150) + " " + special
    gateway = RecordingGateway(
        [{"id": "semantic", "source_quote": source, "requirement": "Раскрыть смысловой блок", "position": "any"}],
        text,
        [{"id": "semantic", "span": special, "position": "any"}],
    )

    result = await _write(gateway, source)

    assert result == text
    assert [call[0] for call in gateway.calls] == ["special_conditions", "writer"]
    assert gateway.calls[1][2] is CoverLetterGenerationDraft


@pytest.mark.asyncio
async def test_whole_letter_literal_span_cannot_exempt_ordinary_words() -> None:
    source = "В письмо добавьте код CODE-42."
    text = "CODE-42 " + " ".join(["обычное"] * 160)
    gateway = RecordingGateway(
        [{"id": "code", "source_quote": source, "requirement": "Добавить код", "literal": "CODE-42", "position": "any"}],
        text,
        [{"id": "code", "span": text, "position": "any"}],
    )

    with pytest.raises(CoverLetterValidationError):
        await _write(gateway, source)

    assert len([call for call in gateway.calls if call[0] == "writer"]) == 3


@pytest.mark.asyncio
async def test_beginning_and_end_conditions_use_actual_text_boundaries() -> None:
    beginning_source = "Start the letter with ALPHA."
    beginning_condition = [{"id": "begin", "source_quote": beginning_source, "requirement": "Start with ALPHA", "literal": "ALPHA", "position": "beginning"}]
    after_greeting = RecordingGateway(beginning_condition, "Здравствуйте!\nALPHA\nОсновной текст.", [{"id": "begin", "span": "ALPHA", "position": "beginning"}])
    with pytest.raises(CoverLetterValidationError):
        await _write(after_greeting, beginning_source)

    end_source = "End the letter with OMEGA."
    end_condition = [{"id": "end", "source_quote": end_source, "requirement": "End with OMEGA", "literal": "OMEGA", "position": "end"}]
    before_signature = RecordingGateway(end_condition, "Основной текст OMEGA\nС уважением, Иван", [{"id": "end", "span": "OMEGA", "position": "end"}])
    with pytest.raises(CoverLetterValidationError):
        await _write(before_signature, end_source)

    exact = RecordingGateway(
        beginning_condition + end_condition,
        "ALPHA\nОсновной текст\nOMEGA",
        [{"id": "begin", "span": "ALPHA", "position": "beginning"}, {"id": "end", "span": "OMEGA", "position": "end"}],
    )
    assert await _write(exact, beginning_source + " " + end_source) == "ALPHA\nОсновной текст\nOMEGA"


@pytest.mark.asyncio
async def test_multiple_beginning_and_end_conditions_are_validated_as_groups() -> None:
    conditions = [
        {"id": "b1", "source_quote": "Begin with ALPHA.", "requirement": "ALPHA first", "literal": "ALPHA", "position": "beginning"},
        {"id": "b2", "source_quote": "Then include BETA at the beginning.", "requirement": "BETA second", "literal": "BETA", "position": "beginning"},
        {"id": "e1", "source_quote": "Include GAMMA at the end.", "requirement": "GAMMA first at end", "literal": "GAMMA", "position": "end"},
        {"id": "e2", "source_quote": "Finish with DELTA.", "requirement": "DELTA last", "literal": "DELTA", "position": "end"},
    ]
    text = "ALPHA BETA Основной текст GAMMA DELTA"
    gateway = RecordingGateway(
        conditions,
        text,
        [{"id": "b1", "span": "ALPHA", "position": "beginning"}, {"id": "b2", "span": "BETA", "position": "beginning"}, {"id": "e1", "span": "GAMMA", "position": "end"}, {"id": "e2", "span": "DELTA", "position": "end"}],
    )

    assert await _write(gateway, " ".join(item["source_quote"] for item in conditions)) == text


@pytest.mark.asyncio
async def test_long_grounded_literal_special_text_over_90_percent_is_accepted() -> None:
    literal = "CODE-42"
    special = literal + " " + " ".join(f"обязательный{i}" for i in range(170))
    source = f'Вставьте в письмо точный текст «{special}».'
    text = special + " хвост"
    gateway = RecordingGateway(
        [{"id": "long", "source_quote": source, "requirement": "Сохранить точный текст", "literal": special, "position": "any"}],
        text,
        [{"id": "long", "span": special, "position": "any"}],
    )

    assert len(special) > len(text) * 0.9
    assert await _write(gateway, source) == text


@pytest.mark.asyncio
async def test_ungrounded_source_quote_and_duplicate_condition_ids_are_rejected() -> None:
    ungrounded = RecordingGateway(
        [{"id": "bad", "source_quote": "Этого нет в вакансии", "requirement": "Невозможное", "position": "any"}],
        "Короткое письмо.",
        [{"id": "bad", "span": "Короткое письмо.", "position": "any"}],
    )
    with pytest.raises(CoverLetterValidationError):
        await _write(ungrounded, "Описание вакансии")

    with pytest.raises(ValidationError):
        SpecialConditionBatch.model_validate({
            "conditions": [
                {"id": "same", "source_quote": "A", "requirement": "A"},
                {"id": "same", "source_quote": "B", "requirement": "B"},
            ],
        })


@pytest.mark.asyncio
async def test_english_start_and_end_requirements_are_extracted_by_structured_contract() -> None:
    description = "Start the cover letter with ALPHA and end it with OMEGA."
    gateway = RecordingGateway(
        [{"id": "start", "source_quote": description, "requirement": "Start with ALPHA", "literal": "ALPHA", "position": "beginning"}, {"id": "end", "source_quote": description, "requirement": "End with OMEGA", "literal": "OMEGA", "position": "end"}],
        "ALPHA\nRelevant experience.\nOMEGA",
        [{"id": "start", "span": "ALPHA", "position": "beginning"}, {"id": "end", "span": "OMEGA", "position": "end"}],
    )

    assert await _write(gateway, description) == "ALPHA\nRelevant experience.\nOMEGA"
    special_call = gateway.calls[0]
    writer_call = gateway.calls[1]
    assert special_call[0] == "special_conditions"
    assert special_call[1]["vacancy_description"] == description
    assert [item["id"] for item in writer_call[1]["special_conditions"]] == ["start", "end"]  # type: ignore[index]
    assert writer_call[2] is CoverLetterGenerationDraft
