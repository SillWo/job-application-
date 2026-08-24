from __future__ import annotations

import pytest

from backend.adapters.hh import locators
from backend.adapters.hh.adapter import HHAdapter
from backend.intelligence.evaluator import evaluate
from backend.schemas.domain import JobPosting, MatchAssessment, ResumeAnalysis


class Locator:
    def __init__(self, value: str | None):
        self.value = value
        self.first = self

    async def count(self):
        return int(self.value is not None)

    async def is_visible(self):
        return True

    async def inner_text(self, timeout=None):
        return self.value


class Page:
    url = "https://hh.ru/vacancy/123"

    def __init__(self, values):
        self.values = values

    def locator(self, selector):
        return Locator(self.values.get(selector))


class MultiLocator:
    def __init__(self, values, visibility=None):
        self.values = values
        self.visibility = visibility or [True] * len(values)
        self.first = self

    async def count(self):
        return len(self.values)

    def nth(self, index):
        locator = Locator(self.values[index])
        locator.is_visible = lambda: self._is_visible(index)
        return locator

    async def _is_visible(self, index):
        return self.visibility[index]


class PageWithDuplicateDescription(Page):
    def locator(self, selector):
        if selector == locators.COMPANY:
            return MultiLocator(["RichBee", "RichBee"], [True, False])
        if selector == locators.DESCRIPTION:
            return MultiLocator(["", "Фактическое описание вакансии"], [True, True])
        return super().locator(selector)


class Link:
    def __init__(self, href, label):
        self.href = href
        self.label = label

    async def get_attribute(self, name):
        return self.href if name == "href" else None

    async def inner_text(self):
        return self.label


class LinkLocator:
    def __init__(self, links):
        self.links = links
        self.first = self

    async def wait_for(self, **kwargs):
        return None

    async def count(self):
        return len(self.links)

    def nth(self, index):
        return self.links[index]


@pytest.mark.asyncio
async def test_visible_job_refs_preserves_page_order_and_deduplicates():
    links = LinkLocator([
        Link("/vacancy/10", "Product Manager"),
        Link("/vacancy/20", "Developer"),
        Link("/vacancy/10", "Product Manager duplicate"),
        Link("/vacancy/30", "Project Manager"),
    ])
    page = type("Page", (), {"url": "https://hh.ru/search/vacancy", "locator": lambda _, __: links})()
    refs = await HHAdapter()._visible_job_refs(page, timeout=100)
    assert [ref.external_id for ref in refs] == ["10", "20", "30"]


@pytest.mark.asyncio
async def test_extract_job_reads_hh_structured_attributes():
    values = {
        locators.VACANCY_TITLE: "IT Project Manager",
        locators.COMPANY: "RichBee",
        locators.DESCRIPTION: "Управление проектами",
        locators.PAYMENT_FREQUENCY: "Выплаты: два раза в месяц",
        locators.WORK_EXPERIENCE: "Опыт работы: 1–3 года",
        locators.EMPLOYMENT: "Полная занятость",
        locators.HIRING_FORMAT: "Оформление: Договор ГПХ",
        locators.WORK_SCHEDULE: "График: 5/2",
        locators.WORKING_HOURS: "Рабочие часы: 8",
        locators.WORK_FORMAT: "Формат работы: удалённо",
    }
    job = await HHAdapter().extract_job(Page(values))
    assert job.model_dump(include={
        "payment_frequency", "required_experience", "employment_type",
        "hiring_format", "work_schedule", "working_hours", "work_format",
    }) == {
        "payment_frequency": "Выплаты: два раза в месяц",
        "required_experience": "Опыт работы: 1–3 года",
        "employment_type": "Полная занятость",
        "hiring_format": "Оформление: Договор ГПХ",
        "work_schedule": "График: 5/2",
        "working_hours": "Рабочие часы: 8",
        "work_format": "Формат работы: удалённо",
    }


@pytest.mark.asyncio
async def test_extract_job_uses_non_empty_duplicate_description():
    values = {
        locators.VACANCY_TITLE: "IT Project Manager",
        locators.COMPANY: "RichBee",
    }
    job = await HHAdapter().extract_job(PageWithDuplicateDescription(values))
    assert job.description == "Фактическое описание вакансии"


class RecordingGateway:
    def __init__(self):
        self.payload = None

    async def structured(self, role, payload, schema):
        self.payload = payload
        def assessment():
            return MatchAssessment(score=2, confidence=1, evidence=["Вакансия: Опыт работы: 1–3 года"])
        return ResumeAnalysis(
            title=assessment(), tasks=assessment(), industry=assessment(),
            required_years=assessment(), languages=assessment(), skills=assessment(),
        )


@pytest.mark.asyncio
async def test_evaluate_passes_structured_attributes_to_resume_analyst():
    job = JobPosting(
        source="hh", url="https://hh.ru/vacancy/123", title="Менеджер", description="Проекты",
        payment_frequency="Выплаты: два раза в месяц", required_experience="Опыт работы: 1–3 года",
        employment_type="Полная занятость", hiring_format="Оформление: Договор ГПХ",
        work_schedule="График: 5/2", working_hours="Рабочие часы: 8", work_format="Формат работы: удалённо",
    )
    gateway = RecordingGateway()
    result = await evaluate(job, {}, [], gateway)
    assert result.score == 74
    assert gateway.payload["job"]["required_experience"] == "Опыт работы: 1–3 года"
    for key in ("payment_frequency", "employment_type", "hiring_format", "work_schedule", "working_hours", "work_format"):
        assert gateway.payload["job"][key] is not None
    assert gateway.payload["job"]["required_experience"] in gateway.payload["job"].values()
    assert result.score_breakdown[3].raw_points == 2
    assert result.score_breakdown[3].evidence == ["Вакансия: Опыт работы: 1–3 года"]
