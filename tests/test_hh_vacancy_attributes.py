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
async def test_personal_recommendations_allow_200_refs_but_search_pages_default_to_100():
    links = LinkLocator([Link(f"/vacancy/{index}", "vacancy") for index in range(250)])
    page = type("Page", (), {"url": "https://hh.ru/", "locator": lambda _, __: links})()
    adapter = HHAdapter()

    personal_refs = await adapter.collect_job_refs(page)
    search_page_refs = await adapter._visible_job_refs(page, timeout=100)

    assert len(personal_refs) == 200
    assert len(search_page_refs) == 100


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
        locators.LOCATION: "Москва, улица Вавилова, 19",
    }
    job = await HHAdapter().extract_job(Page(values))
    assert job.model_dump(include={
        "payment_frequency", "required_experience", "employment_type",
        "hiring_format", "work_schedule", "working_hours", "work_format", "location",
    }) == {
        "payment_frequency": "Выплаты: два раза в месяц",
        "required_experience": "Опыт работы: 1–3 года",
        "employment_type": "Полная занятость",
        "hiring_format": "Оформление: Договор ГПХ",
        "work_schedule": "График: 5/2",
        "working_hours": "Рабочие часы: 8",
        "work_format": "Формат работы: удалённо",
        "location": "Москва, улица Вавилова, 19",
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
            tasks=assessment(),
            skills=[],
            experience_depth=assessment(),
            role_match=assessment(),
            industry=assessment(),
            special_requirements=assessment(),
        )


@pytest.mark.parametrize("text,expected", [
    ("до 36\u00a0000 ₽ за месяц, на руки", (None, 36000, "RUB", False)),
    ("от 60\u202f000 до 90\u202f000 ₽ в месяц, до вычета налогов", (60000, 90000, "RUB", True)),
    ("60 000–90 000 руб.", (60000, 90000, "RUB", None)),
    ("от 60 000 ₽", (60000, None, "RUB", None)),
    ("60 000 ₽ за месяц", (60000, 60000, "RUB", None)),
    ("до 36 тыс. руб/мес", (None, 36000, "RUB", None)),
    ("от 1 000 до 2 000 USD за месяц", (1000, 2000, "USD", None)),
    ("до 500 € в месяц", (None, 500, "EUR", None)),
    ("500 ₽ за час", None),
    ("5 000 ₽ за смену", None),
    ("1 000 000 ₽ в год", None),
    ("По договорённости", None),
    ("до 36 000 неизвестных единиц", None),
    (None, None),
])
async def test_salary_survives_extraction_and_model_payload(text, expected):
    job = await HHAdapter().extract_job(Page({
        locators.VACANCY_TITLE: "Стажёр",
        locators.COMPANY: "Компания",
        locators.DESCRIPTION: "Координация проектов",
        locators.SALARY: text,
    }))
    gateway = RecordingGateway()
    await evaluate(job, {}, [], gateway)
    if expected is None:
        assert job.salary is None
        assert gateway.payload["job"]["salary"] is None
    else:
        assert (job.salary.minimum, job.salary.maximum, job.salary.currency, job.salary.gross) == expected
        assert gateway.payload["job"]["salary"] == job.salary.model_dump()
    if text:
        assert " ".join(text.split()) in gateway.payload["job"]["description"]


async def test_salary_skips_hidden_and_empty_duplicate_blocks():
    class DuplicateSalaryPage(Page):
        def locator(self, selector):
            if selector == locators.SALARY:
                return MultiLocator(["999 000 ₽", "", "до 36 000 ₽ за месяц"], [False, True, True])
            return super().locator(selector)

    job = await HHAdapter().extract_job(DuplicateSalaryPage({
        locators.VACANCY_TITLE: "Стажёр",
        locators.COMPANY: "Компания",
        locators.DESCRIPTION: "Координация проектов",
    }))
    assert job.salary.maximum == 36000
    assert "999 000" not in job.description


@pytest.mark.parametrize("unreadable", ["", "timeout"])
async def test_unreadable_salary_does_not_silently_become_unspecified(unreadable):
    class UnreadableSalaryPage(Page):
        def locator(self, selector):
            locator = super().locator(selector)
            if selector == locators.SALARY and unreadable == "timeout":
                async def fail(**kwargs):
                    raise TimeoutError("Block detached during extraction")
                locator.inner_text = fail
            return locator

    with pytest.raises(ValueError, match="блок зарплаты"):
        await HHAdapter().extract_job(UnreadableSalaryPage({
            locators.VACANCY_TITLE: "Стажёр", locators.COMPANY: "Компания",
            locators.DESCRIPTION: "Координация проектов", locators.SALARY: unreadable,
        }))


async def test_extracted_36000_is_blocked_by_60000_salary_policy():
    job = await HHAdapter().extract_job(Page({
        locators.VACANCY_TITLE: "Стажёр",
        locators.COMPANY: "Компания",
        locators.DESCRIPTION: "Координация проектов",
        locators.SALARY: "до 36 000 ₽ за месяц, на руки",
    }))
    result = await evaluate(job, {}, [], RecordingGateway(), preference_policy={
        "desired_salary": {"minimum_monthly_amount": 60000, "currency": "RUB"},
    })
    assert result.decision == "skip"
    assert "salary_below_preference" in result.hard_rule_violations


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
    assert result.score == 45
    assert gateway.payload["job"]["required_experience"] == "Опыт работы: 1–3 года"
    for key in ("payment_frequency", "employment_type", "hiring_format", "work_schedule", "working_hours", "work_format"):
        assert gateway.payload["job"][key] is not None
    assert gateway.payload["job"]["required_experience"] in gateway.payload["job"].values()
    experience_row = next(row for row in result.score_breakdown if row.key == "experience_depth")
    assert experience_row.raw_points == 2
    assert experience_row.evidence == ["Вакансия: Опыт работы: 1–3 года"]
