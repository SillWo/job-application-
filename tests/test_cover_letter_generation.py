import re

import pytest

from backend.intelligence.letter_claims import _has_candidate_fact_claim, _semantic_strings
from backend.intelligence.letter_writer import (
    CoverLetterGenerationDraft,
    CoverLetterValidationError,
    SpecialConditionBatch,
    _private_values,
    _professional_model_payload,
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
        if role == "letter_claim_check":
            return _claim_check(schema, payload)
        return self.draft_factory(schema, payload, len(self.calls))


def _claim_check(schema, payload):
    letter = payload["letter"]
    evidence = []
    unsupported = []
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", letter):
        if not _has_candidate_fact_claim(sentence):
            continue
        match = next((
            (index, source)
            for index, resume in enumerate(payload["resumes"])
            for source in _semantic_strings(resume)
            if source.casefold() in sentence.casefold()
        ), None)
        if match is None:
            unsupported.append(sentence.strip())
        else:
            evidence.append({
                "claim_span": sentence.strip(), "resume_index": match[0], "source_quote": match[1],
            })
    return schema.model_validate({
        "all_candidate_claims_supported": not unsupported,
        "confidence": 1,
        "unsupported_claims": unsupported,
        "evidence": evidence,
    })


def _job(description="Описание вакансии"):
    return JobPosting(source="test", url="https://example.test", title="Аналитик", company="Тест", description=description)


def test_professional_payload_keeps_named_professional_items_and_redacts_free_text():
    source = {
        "identity": {"full_name": {"value": "Иван Иванов", "availability": "present"}},
        "contacts": {
            "phone": {"value": "+7 999 123-45-67", "availability": "present"},
            "email": {"value": "ivan@example.test", "availability": "present"},
            "messengers": {"value": ["https://t.me/ivan"], "availability": "present"},
        },
    }
    resume = {
        "identity": source["identity"],
        "contacts": source["contacts"],
        "about": {"value": "Иван Иванов: SQL, ivan@example.test, +7 999 123-45-67 https://t.me/ivan", "availability": "present"},
        "experience": [{
            "company": {"value": "ООО Пример", "availability": "present"},
            "company_url": {"value": "https://company.example.test", "availability": "present"},
            "duties": {"value": "Иван Иванов улучшил отчёты; бюджет 2 млн рублей", "availability": "present"},
        }],
        "skills": [{"name": {"value": "SQL", "availability": "present"}}],
        "courses": [{"name": {"value": "Курс SQL", "availability": "present"}}],
        "projects": [{"name": {"value": "Проект Альфа", "availability": "present"}}],
        "additional_sections": [{"name": "Профессиональные интересы", "content": {"value": "Иван Иванов изучает SQL", "availability": "present"}}],
    }
    original = __import__("copy").deepcopy(resume)

    payload = _professional_model_payload(resume, [source, resume])

    assert "identity" not in payload and "contacts" not in payload
    assert payload["skills"][0]["name"]["value"] == "SQL"
    assert payload["courses"][0]["name"]["value"] == "Курс SQL"
    assert payload["projects"][0]["name"]["value"] == "Проект Альфа"
    assert payload["additional_sections"][0]["name"] == "Профессиональные интересы"
    assert "Иван Иванов" not in payload["about"]["value"]
    assert "ivan@example.test" not in payload["about"]["value"]
    assert "+7 999 123-45-67" not in payload["about"]["value"]
    assert "t.me/ivan" not in payload["about"]["value"]
    assert payload["experience"][0]["company_url"]["value"] == "https://company.example.test"
    assert "2 млн рублей" in payload["experience"][0]["duties"]["value"]
    assert resume == original
    assert "full_name" not in _private_values({"skills": [{"name": "Python"}]})


def test_professional_payload_handles_flat_normalized_fields_without_dropping_names():
    payload = _professional_model_payload({
        "skills": [{"name": "SQL"}],
        "courses": [{"name": "Курс аналитики"}],
        "projects": [{"name": "Дашборд продаж"}],
        "additional_sections": [{"name": "Дополнительно", "content": "Email me at person@example.test"}],
        "about": "Мой телефон +7 999 123-45-67",
    }, {"full_name": "Иван Иванов", "phone": "+7 999 123-45-67", "email": "person@example.test"})

    assert payload["skills"] == [{"name": "SQL"}]
    assert payload["courses"] == [{"name": "Курс аналитики"}]
    assert payload["projects"] == [{"name": "Дашборд продаж"}]
    assert payload["additional_sections"][0]["name"] == "Дополнительно"
    assert "person@example.test" not in payload["additional_sections"][0]["content"]
    assert "+7 999 123-45-67" not in payload["about"]


def test_professional_payload_redacts_grounded_messenger_handles_from_about():
    resume = {
        "contacts": {
            "messengers": ["https://t.me/synthetic_handle"],
            "links": ["https://vk.com/synthetic_vk"],
        },
        "about": "Experienced analyst. Telegram: @synthetic_handle; VK: @synthetic_vk",
    }
    payload = _professional_model_payload(resume, resume)

    assert "Experienced analyst." in payload["about"]
    assert "@synthetic_handle" not in payload["about"]
    assert "@synthetic_vk" not in payload["about"]


def test_professional_payload_redacts_inline_vk_url_and_handle_without_contact_links():
    payload = _professional_model_payload({
        "contacts": {"links": None},
        "about": "Experienced analyst. ВК - https://vk.com/synthetic_user (@synthetic_user). Increased revenue 25%.",
    })

    assert "Experienced analyst." in payload["about"]
    assert "Increased revenue 25%." in payload["about"]
    assert "vk.com/synthetic_user" not in payload["about"]
    assert "@synthetic_user" not in payload["about"]


def test_professional_payload_keeps_flat_grounded_handles_and_redacts_inline_vk_handle():
    private_source = {
        "contacts": {
            "messengers": {"value": ["https://t.me/synthetic_telegram", "@synthetic_telegram"], "availability": "present"},
            "links": {"value": None, "availability": "not_provided"},
        },
    }
    resume = {
        "about": {"value": "Experienced analyst. Telegram @synthetic_telegram; VK https://vk.com/vk_handle (@vk_handle).", "availability": "present"},
        "skills": [{"name": {"value": "Python", "availability": "present"}}],
        "courses": [{"name": {"value": "SQL Analytics", "availability": "present"}}],
        "experience": [{"company_url": {"value": "https://company.example.test", "availability": "present"}}],
    }

    payload = _professional_model_payload(resume, private_source)

    assert "Experienced analyst." in payload["about"]["value"]
    assert "@synthetic_telegram" not in payload["about"]["value"]
    assert "@vk_handle" not in payload["about"]["value"]
    assert payload["skills"][0]["name"]["value"] == "Python"
    assert payload["courses"][0]["name"]["value"] == "SQL Analytics"
    assert payload["experience"][0]["company_url"]["value"] == "https://company.example.test"


@pytest.mark.asyncio
async def test_writer_payload_keeps_professional_names_and_excludes_profile_contacts():
    captured = {}

    def draft(schema, payload, _count):
        captured.update(payload)
        return schema.model_validate({"text": "Подхожу под задачи вакансии.", "fulfilled_special_conditions": []})

    profile = {
        "gender": "female", "full_name": "Иван Иванов", "phone": "+7 999 123-45-67",
        "email": "ivan@example.test", "messengers": ["https://t.me/synthetic_handle"],
    }
    resume = {
        "identity": {"full_name": "Иван Иванов"},
        "contacts": {"phone": "+7 999 123-45-67", "messengers": ["https://t.me/synthetic_handle"]},
        "skills": [{"name": "Python"}],
        "courses": [{"name": "Курс анализа данных"}],
        "projects": [{"name": "Сервис отчётности"}],
        "about": "Иван Иванов. Telegram: @synthetic_handle. Анализ данных и Python.",
    }

    await write_cover_letter(_job(), profile, [resume], Gateway(draft))

    model_resume = captured["resumes"][0]
    assert model_resume["skills"][0]["name"] == "Python"
    assert model_resume["courses"][0]["name"] == "Курс анализа данных"
    assert model_resume["projects"][0]["name"] == "Сервис отчётности"
    for personal_value in ("Иван Иванов", "+7 999 123-45-67", "ivan@example.test", "@synthetic_handle"):
        assert personal_value not in str(model_resume)


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
async def test_writer_uses_fresh_generation_with_safe_repair_category():
    class FreshGateway(Gateway):
        def __init__(self):
            super().__init__(lambda *_: None)
            self.fresh_calls = []

        async def fresh_generation(self, role, payload, schema, *, correction_category, generation):
            self.fresh_calls.append((role, payload, correction_category, generation))
            return schema.model_validate({
                "text": "Полный текст письма без пустых слотов.",
                "fulfilled_special_conditions": [],
            })

        async def structured(self, role, payload, schema):
            self.calls.append((role, payload, schema))
            if role == "special_conditions":
                return SpecialConditionBatch(conditions=[])
            if role == "letter_claim_check":
                return _claim_check(schema, payload)
            return schema.model_validate({"text": "Я [ФИО]", "fulfilled_special_conditions": []})

    gateway = FreshGateway()
    result = await write_cover_letter(
        _job(), {"gender": "female"}, [{"name": "Резюме"}], gateway,
    )

    assert result == "Полный текст письма без пустых слотов."
    assert gateway.fresh_calls[0][2:] == ("requirements", 1)
    assert "Я [ФИО]" not in str(gateway.fresh_calls[0][1])


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
            if role == "letter_claim_check":
                return _claim_check(schema, payload)
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
            if role == "letter_claim_check":
                return _claim_check(schema, payload)
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
            if role == "letter_claim_check":
                return _claim_check(schema, payload)
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
            if role == "letter_claim_check":
                return _claim_check(schema, payload)
            return CoverLetterGenerationDraft(
                text="Готов обсудить задачи вакансии.",
                fulfilled_special_conditions=[{"id": "unknown", "span": "Готов"}],
            )

    gateway = BadGateway(lambda *_: None)
    with pytest.raises(CoverLetterValidationError):
        await write_cover_letter(_job(), {"gender": "female"}, [{"name": "Резюме"}], gateway)
    assert len([role for role, *_ in gateway.calls if role == "writer"]) == 3
