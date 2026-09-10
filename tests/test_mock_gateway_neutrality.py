import pytest

from backend.intelligence.gateway import ModelGateway
from backend.intelligence.letter_writer import (
    CoverLetterValidationError,
    _finish_cover_letter,
    write_cover_letter,
)
from backend.schemas.domain import CoverLetterDraft, JobPosting, ResumeImportData


@pytest.mark.asyncio
async def test_mock_resume_import_extracts_any_desired_title():
    result = await ModelGateway(provider="mock").structured(
        "profile",
        {"resume_text": "Иван Иванов\nЖелаемая должность: Python-разработчик\nОпыт работы"},
        ResumeImportData,
    )
    assert result.resume.desired_title == "Python-разработчик"


@pytest.mark.asyncio
async def test_mock_cover_letter_is_neutral_and_uses_vacancy_title():
    result = await ModelGateway(provider="mock").structured(
        "letter_writer",
        {"vacancy": {"title": "Инженер по тестированию"}},
        CoverLetterDraft,
    )
    assert "Инженер по тестированию" in result.text
    assert "product" not in result.text.lower()
    assert "продукт" not in result.text.lower()


def test_cover_letter_validation_does_not_flatten_or_invent_closing():
    draft = " ".join(["слово"] * 160)
    with pytest.raises(CoverLetterValidationError):
        _finish_cover_letter(draft, {"contacts": {"messengers": ["https://t.me/example"]}})


def test_cover_letter_does_not_invent_messenger_link_when_contacts_are_empty():
    result = _finish_cover_letter("Короткий текст.", {"contacts": {"messengers": []}})
    assert result == "Короткий текст."


@pytest.mark.asyncio
async def test_write_cover_letter_passes_new_generation_contract():
    class FakeGateway:
        async def structured(self, role, payload, response_model):
            if role == "special_conditions":
                return response_model(conditions=[])
            assert role == "writer"
            requirements = payload["requirements"]
            assert "не более 150 слов" in requirements
            assert "квадратные скобки" in requirements
            assert "Особые условия работодателя" in requirements
            return response_model(
                text="Мне интересна вакансия и задачи. Компания близка по подходу. Мой опыт подходит.",
                fulfilled_special_conditions=[],
            )

    result = await write_cover_letter(
        JobPosting(source="mock", url="https://example.test/job", title="Тестировщик", company="Компания", description="Задачи"),
        {"full_name": "Иван Иванов", "gender": "male", "contacts": {"messengers": ["https://t.me/ivan"]}},
        [{"name": "Резюме"}],
        FakeGateway(),
    )
    assert result == "Мне интересна вакансия и задачи. Компания близка по подходу. Мой опыт подходит."


def test_cover_letter_does_not_duplicate_greeting():
    result = _finish_cover_letter("Здравствуйте!\n\nКороткий текст.", {})
    assert result.startswith("Здравствуйте!\n\nКороткий текст.")
    assert result.count("Здравствуйте!") == 1
