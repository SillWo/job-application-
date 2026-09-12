import pytest

from backend.intelligence.letter_writer import (
    CoverLetterGenerationDraft,
    CoverLetterValidationError,
    SpecialConditionBatch,
    validate_cover_letter,
    write_cover_letter,
)
from backend.intelligence.prompts import COVER_LETTER_SYSTEM_PROMPT
from backend.schemas.domain import JobPosting


class Gateway:
    def __init__(self, draft_factory):
        self.draft_factory = draft_factory
        self.calls = []

    async def structured(self, role, payload, schema):
        self.calls.append((role, payload, schema))
        if role == "special_conditions":
            return schema.model_validate({"conditions": []})
        return self.draft_factory(schema, payload, len(self.calls))


def _job(description="Описание вакансии"):
    return JobPosting(source="test", url="https://example.test", title="Аналитик", company="Тест", description=description)


@pytest.mark.asyncio
async def test_writer_requires_explicit_profile_gender_before_model_call():
    gateway = Gateway(lambda *_: None)
    with pytest.raises(CoverLetterValidationError, match="Укажите пол"):
        await write_cover_letter(_job(), {"full_name": "Иван Иванов"}, [{"skills": ["SQL"]}], gateway)
    assert gateway.calls == []


@pytest.mark.asyncio
async def test_auto_mode_ignores_stale_template_and_sends_full_description():
    captured = {}

    def draft(schema, payload, _count):
        captured.update(payload)
        return schema.model_validate({"text": "Готов обсудить задачи вакансии.", "fulfilled_special_conditions": []})

    gateway = Gateway(draft)
    result = await write_cover_letter(
        _job("начало " + "x" * 10000 + " конец"),
        {"full_name": "Иван Иванов", "gender": "male"},
        [{"skills": ["SQL"]}],
        gateway,
        cover_letter_auto=True,
        cover_letter_template="Я [ФИО] и старый шаблон",
    )
    assert result == "Готов обсудить задачи вакансии."
    assert captured["cover_letter_template"] == ""
    assert captured["vacancy"]["description"].startswith("начало ")
    assert captured["vacancy"]["description"].endswith(" конец")
    assert len(captured["vacancy"]["description"]) > 10000
    assert "## 20. Финальная внутренняя проверка" in COVER_LETTER_SYSTEM_PROMPT


@pytest.mark.asyncio
async def test_custom_word_limit_is_forwarded_and_used_for_validation():
    text = " ".join(["слово"] * 151)

    def draft(schema, payload, _count):
        assert payload["cover_letter_max_words"] == 200
        assert "не более 200 слов" in payload["requirements"]
        return schema.model_validate({"text": text, "fulfilled_special_conditions": []})

    result = await write_cover_letter(
        _job(), {"gender": "female"}, [{"name": "Резюме"}], Gateway(draft),
        cover_letter_max_words=200,
    )
    assert result == text


@pytest.mark.asyncio
async def test_default_word_limit_remains_150_words():
    text = " ".join(["слово"] * 151)
    gateway = Gateway(lambda schema, _payload, _count: schema.model_validate({
        "text": text, "fulfilled_special_conditions": [],
    }))
    with pytest.raises(CoverLetterValidationError):
        await write_cover_letter(_job(), {"gender": "female"}, [{"name": "Резюме"}], gateway)
    assert len([role for role, *_ in gateway.calls if role == "writer"]) == 3


@pytest.mark.asyncio
async def test_ordinary_quoted_site_instruction_is_not_letter_condition():
    captured = {}

    def draft(schema, payload, _count):
        captured.update(payload)
        return schema.model_validate({"text": "Готов обсудить задачи вакансии.", "fulfilled_special_conditions": []})

    gateway = Gateway(draft)
    await write_cover_letter(
        _job("Добавьте компанию «Альфа» в избранное на сайте."),
        {"full_name": "Иван Иванов", "gender": "male"},
        [{"skills": ["SQL"]}],
        gateway,
    )
    assert "Альфа" not in captured["requirements"]


@pytest.mark.asyncio
async def test_custom_template_is_forwarded_and_unresolved_slot_is_repaired():
    seen = []

    def draft(schema, payload, _count):
        seen.append(payload)
        text = "Я [ФИО]" if len(seen) == 1 else "Я Иван Иванов и хочу обсудить эту вакансию."
        return schema.model_validate({"text": text, "fulfilled_special_conditions": []})

    gateway = Gateway(draft)
    result = await write_cover_letter(
        _job(),
        {"full_name": "Иван Иванов", "gender": "male"},
        [{"skills": ["SQL"]}],
        gateway,
        cover_letter_auto=False,
        cover_letter_template="Я [ФИО] и мой опыт [сильная сторона]",
    )
    assert result == "Я Иван Иванов и хочу обсудить эту вакансию."
    assert seen[0]["cover_letter_template"] == "Я [ФИО] и мой опыт [сильная сторона]"
    assert len(seen) == 2
    assert "исправительн" in seen[1]["requirements"].casefold()


@pytest.mark.asyncio
async def test_special_span_is_exempt_from_word_limit_but_grounded_to_source():
    source = "В начале письма добавьте этот блок."
    special = "Особый блок " + " ".join(["важное"] * 169)
    text = " ".join(["факт"] * 150) + " " + special

    class SpecialGateway(Gateway):
        async def structured(self, role, payload, schema):
            self.calls.append((role, payload, schema))
            if role == "special_conditions":
                return SpecialConditionBatch.model_validate({
                    "conditions": [{
                        "id": "s1", "source_quote": source,
                        "requirement": "Добавить специальный блок", "literal": None,
                        "position": "any",
                    }]
                })
            return CoverLetterGenerationDraft.model_validate({
                "text": text,
                "fulfilled_special_conditions": [{"id": "s1", "span": special, "position": "any"}],
            })

    result = await write_cover_letter(_job(source), {"gender": "female"}, [{"name": "Резюме"}], SpecialGateway(lambda *_: None))
    assert result == text


@pytest.mark.asyncio
async def test_factual_question_answer_is_semantic_not_unverified_literal():
    source = "Please answer in your cover letter: What's the capital of the United Kingdom?"

    class EnglishQuestionGateway(Gateway):
        async def structured(self, role, payload, schema):
            self.calls.append((role, payload, schema))
            if role == "special_conditions":
                # Simulate a provider that initially mistakes the answer for
                # a literal employer token. The writer must still receive the
                # grounded question and answer it.
                return SpecialConditionBatch(conditions=[{
                    "id": "question_1",
                    "source_quote": source,
                    "requirement": source,
                    "literal": "London",
                    "position": "any",
                }])
            return CoverLetterGenerationDraft(
                text="Здравствуйте! London. С уважением, Иван Иванов.",
                fulfilled_special_conditions=[{
                    "id": "question_1", "span": "London", "position": "any",
                }],
            )

    gateway = EnglishQuestionGateway(lambda *_: None)
    result = await write_cover_letter(
        _job(source),
        {"full_name": "Иван Иванов", "gender": "male"},
        [{"name": "Резюме"}],
        gateway,
    )
    assert "London" in result
    writer_payload = next(payload for role, payload, _schema in gateway.calls if role == "writer")
    assert writer_payload["special_conditions"][0]["literal"] is None


@pytest.mark.asyncio
async def test_quoted_employer_token_remains_verbatim_requirement():
    source = "В сопроводительном письме укажите слово «ORBITA»."

    class TokenGateway(Gateway):
        async def structured(self, role, payload, schema):
            self.calls.append((role, payload, schema))
            if role == "special_conditions":
                return SpecialConditionBatch(conditions=[{
                    "id": "token_1", "source_quote": source,
                    "requirement": "Указать слово ORBITA", "literal": "ORBITA",
                    "position": "any",
                }])
            return CoverLetterGenerationDraft(
                text="Здравствуйте! ORBITA. С уважением, Иван Иванов.",
                fulfilled_special_conditions=[{"id": "token_1", "span": "ORBITA"}],
            )

    result = await write_cover_letter(
        _job(source), {"full_name": "Иван Иванов", "gender": "male"},
        [{"name": "Резюме"}], TokenGateway(lambda *_: None),
    )
    assert "ORBITA" in result


def test_invalid_text_is_rejected_without_rewriting():
    valid, reason = validate_cover_letter("Начало [ФИО].", "")
    assert not valid
    assert "квадратн" in reason


@pytest.mark.asyncio
async def test_unknown_fulfilled_condition_id_is_rejected_even_without_extracted_conditions():
    class BadGateway(Gateway):
        async def structured(self, role, payload, schema):
            self.calls.append((role, payload, schema))
            if role == "special_conditions":
                return SpecialConditionBatch(conditions=[])
            return CoverLetterGenerationDraft(
                text="Готов обсудить задачи вакансии.",
                fulfilled_special_conditions=[{"id": "unknown", "span": "Готов"}],
            )

    gateway = BadGateway(lambda *_: None)
    with pytest.raises(CoverLetterValidationError):
        await write_cover_letter(_job(), {"gender": "female"}, [{"name": "Резюме"}], gateway)
    assert len([role for role, *_ in gateway.calls if role == "writer"]) == 3
