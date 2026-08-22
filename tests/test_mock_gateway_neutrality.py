import pytest

from backend.intelligence.gateway import ModelGateway
from backend.intelligence.letter_writer import _finish_cover_letter, write_cover_letter
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


def test_cover_letter_closing_uses_only_profile_messengers_and_stays_within_limit():
    draft = " ".join(["слово"] * 130)
    result = _finish_cover_letter(
        draft,
        {"contacts": {"messengers": ["https://t.me/example", "@example"]}},
    )
    assert result.endswith(
        "Буду рад продолжить общение с вами в этом чате или в мессенджерах - "
        "https://t.me/example, @example"
    )
    assert len(result.split()) <= 100


def test_cover_letter_does_not_invent_messenger_link_when_contacts_are_empty():
    result = _finish_cover_letter("Короткий текст.", {"contacts": {"messengers": []}})
    assert result.endswith(
        "Буду рад продолжить общение с вами в этом чате или в мессенджерах -"
    )
    assert "t.me" not in result


@pytest.mark.asyncio
async def test_write_cover_letter_passes_three_blocks_and_adds_contractual_closing():
    class FakeGateway:
        async def structured(self, role, payload, response_model):
            assert role == "writer"
            requirements = payload["requirements"]
            assert "не более 70 слов" in requirements
            assert "почему понравилась вакансия" in requirements
            assert "почему понравилась компания" in requirements
            assert "преимущества кандидата" in requirements
            return response_model(text="Мне интересна вакансия и задачи. Компания близка по подходу. Мой опыт подходит.")

    result = await write_cover_letter(
        JobPosting(source="mock", url="https://example.test/job", title="Тестировщик", company="Компания", description="Задачи"),
        {"full_name": "Иван Иванов", "contacts": {"messengers": ["https://t.me/ivan"]}},
        [{"name": "Резюме"}],
        FakeGateway(),
    )
    assert result.endswith(
        "Буду рад продолжить общение с вами в этом чате или в мессенджерах - https://t.me/ivan"
    )
    assert len(result.split()) <= 100
