from __future__ import annotations

import pytest

from backend.adapters.base.errors import JobDescriptionUnavailable
from backend.adapters.zarplata import locators
from backend.adapters.zarplata.adapter import ZarplataAdapter
from backend.orchestrator.recovery import AuthenticationPending
from backend.schemas.domain import ApplicationPlan, FormAnswer

VACANCY_ID = "138130289"
VACANCY_URL = f"https://krasnoyarsk.zarplata.ru/vacancy/{VACANCY_ID}"
RESPONSE_URL = (
    "https://krasnoyarsk.zarplata.ru/applicant/vacancy_response"
    f"?vacancyId={VACANCY_ID}"
)
LETTER = "Synthetic route fixture letter"


class Locator:
    def __init__(self, page, selector):
        self.page = page
        self.selector = selector
        self.first = self

    async def count(self):
        if self.selector == locators.APPLICATION_CONTROL:
            return 0
        return int(bool(self.page.visible.get(self.selector, False)))

    async def is_visible(self):
        return bool(self.page.visible.get(self.selector, False))

    async def is_enabled(self):
        return bool(self.page.enabled.get(self.selector, True))

    async def click(self):
        self.page.clicks.append(self.selector)
        if self.selector == locators.RESPONSE_BUTTON:
            self.page.visible[self.selector] = False
            self.page.url = RESPONSE_URL
        elif self.selector == locators.COVER_LETTER_TOGGLE:
            self.page.visible[self.selector] = False
            self.page.visible[locators.COVER_LETTER_INPUT] = True
            self.page.visible[locators.RESPONSE_SUBMIT] = True
        elif self.selector == locators.RESPONSE_SUBMIT:
            self.page.visible[self.selector] = False
            self.page.visible[locators.SUBMISSION_CONFIRMED] = True

    async def fill(self, value):
        self.page.values[self.selector] = value

    async def input_value(self):
        return self.page.values.get(self.selector, "")

    async def all_text_contents(self):
        return []

    async def inner_text(self):
        return self.page.body_text if self.selector == "body" else ""


class Page:
    def __init__(self, url=VACANCY_URL, visible=None):
        self.url = url
        self.visible = visible or {}
        self.enabled = {}
        self.clicks = []
        self.values = {}
        self.body_text = ""

    def locator(self, selector):
        return Locator(self, selector)

    async def wait_for_timeout(self, _milliseconds):
        return None


class MetadataControl:
    def __init__(self, *, control_type, name, visible=True):
        self.attributes = {"type": control_type, "name": name}
        self.visible = visible
        self.value = ""

    async def is_visible(self):
        return self.visible

    async def get_attribute(self, name):
        return self.attributes.get(name)

    async def fill(self, value):
        self.value = value

    async def input_value(self):
        return self.value


class MetadataControlList:
    def __init__(self, controls):
        self.controls = controls

    async def count(self):
        return len(self.controls)

    def nth(self, index):
        return self.controls[index]


class TextList:
    def __init__(self, values):
        self.values = values

    async def all_text_contents(self):
        return self.values


class MetadataQuestionPage(Page):
    def __init__(self, controls, prompts, url=VACANCY_URL):
        super().__init__(url)
        self.controls = controls
        self.prompts = prompts

    def locator(self, selector):
        if selector == locators.APPLICATION_CONTROL:
            return MetadataControlList(self.controls)
        if selector == locators.TASK_QUESTION:
            return TextList(self.prompts)
        if selector == locators.APPLICATION_QUESTION:
            return TextList([])
        return super().locator(selector)


def expected_adapter():
    adapter = ZarplataAdapter()
    adapter._expected_job_id = VACANCY_ID
    adapter._submission_timeout_ms = 0
    return adapter


@pytest.mark.asyncio
async def test_response_redirect_reads_and_submits_full_page_letter_form_once():
    page = Page(visible={
        locators.RESPONSE_BUTTON: True,
        locators.COVER_LETTER_TOGGLE: True,
        locators.RESPONSE_SUBMIT: True,
        locators.COVER_LETTER_INPUT: False,
    })
    adapter = expected_adapter()
    plan = ApplicationPlan(
        vacancy_id=VACANCY_ID,
        resume_file="resume.pdf",
        submission_allowed=True,
        cover_letter=LETTER,
    )

    form = await adapter.open_application(page)
    assert form.requires_cover_letter
    assert page.url == RESPONSE_URL

    filled = await adapter.fill_application(page, plan)
    assert filled.success
    assert page.values[locators.COVER_LETTER_INPUT] == LETTER

    result = await adapter.submit_application(page)
    assert result.status == "submitted"
    assert page.clicks == [
        locators.RESPONSE_BUTTON,
        locators.COVER_LETTER_TOGGLE,
        locators.RESPONSE_SUBMIT,
    ]
    assert page.clicks.count(locators.RESPONSE_BUTTON) == 1
    assert adapter.get_submission_progress()["cover_letter_confirmed"] is True


@pytest.mark.parametrize(
    "url",
    [
        "https://krasnoyarsk.zarplata.ru/applicant/vacancy_response",
        "https://krasnoyarsk.zarplata.ru/applicant/vacancy_response?vacancyId=138130288",
        "https://krasnoyarsk.zarplata.ru/applicant/vacancy_response?vacancyId=abc",
        "https://krasnoyarsk.zarplata.ru/applicant/vacancy_response?vacancyId=１３８１３０２８９",
        "https://krasnoyarsk.zarplata.ru/applicant/vacancy_response?vacancyId=138130289&vacancyId=138130289",
        "https://krasnoyarsk.zarplata.ru/applicant/other?vacancyId=138130289",
        "https://evil.example/applicant/vacancy_response?vacancyId=138130289",
        "http://krasnoyarsk.zarplata.ru/applicant/vacancy_response?vacancyId=138130289",
    ],
)
@pytest.mark.asyncio
async def test_response_route_rejects_unproven_identity_or_origin(url):
    page = Page(url)
    adapter = expected_adapter()

    error = (
        AuthenticationPending
        if "evil.example" in url or url.startswith("http:")
        else JobDescriptionUnavailable
    )
    with pytest.raises(error):
        await adapter.read_application(page)


@pytest.mark.asyncio
async def test_response_route_accepts_exact_id_and_unrelated_query_keys():
    page = Page(
        "https://krasnoyarsk.zarplata.ru/applicant/vacancy_response"
        f"?source=fixture&vacancyId={VACANCY_ID}&mode=apply"
    )

    form = await expected_adapter().read_application(page)

    assert not form.requires_cover_letter


@pytest.mark.asyncio
async def test_response_route_requires_an_expected_job_id():
    page = Page(RESPONSE_URL)

    with pytest.raises(JobDescriptionUnavailable):
        await ZarplataAdapter().read_application(page)


@pytest.mark.parametrize("invalid_id", ["vacancy-1", "１"])
@pytest.mark.asyncio
async def test_application_route_rejects_invalid_expected_job_id(invalid_id):
    page = Page(VACANCY_URL)
    adapter = ZarplataAdapter()
    adapter._expected_job_id = invalid_id

    with pytest.raises(JobDescriptionUnavailable):
        await adapter.read_application(page)


@pytest.mark.asyncio
async def test_vacancy_extraction_validator_still_rejects_response_form_route():
    page = Page(RESPONSE_URL)

    with pytest.raises(JobDescriptionUnavailable):
        await expected_adapter()._validate_vacancy_page(page)


@pytest.mark.asyncio
async def test_ordinary_vacancy_route_remains_valid_for_application_read():
    page = Page(VACANCY_URL)

    form = await expected_adapter().read_application(page)

    assert not form.requires_cover_letter


@pytest.mark.asyncio
async def test_observed_textareas_bind_to_prompts_and_fill_exact_answers():
    prompts = ["Опишите опыт работы", "Почему заинтересовала вакансия?"]
    controls = [
        MetadataControl(control_type="textarea", name="task_386366758_text"),
        MetadataControl(control_type="textarea", name="task_386366759_text"),
    ]
    page = MetadataQuestionPage(controls, prompts)
    adapter = expected_adapter()
    form = await adapter.read_application(page)

    assert [field.kind for field in form.fields] == ["text", "text"]
    assert [field.label for field in form.fields] == prompts
    answers = ["Синтетический ответ об опыте", "Синтетический ответ о вакансии"]
    plan = ApplicationPlan(
        vacancy_id=int(VACANCY_ID),
        resume_file="resume.pdf",
        submission_allowed=True,
        form_answers={
            field.id: FormAnswer(field=field, values=[answer], source="fixture")
            for field, answer in zip(form.fields, answers, strict=True)
        },
    )

    result = await adapter.fill_application(page, plan)

    assert result.success
    assert result.answered_fields == [field.id for field in form.fields]
    assert [control.value for control in controls] == answers


@pytest.mark.asyncio
async def test_unobserved_control_type_remains_unsupported():
    prompt = "Неизвестный тип поля"
    page = MetadataQuestionPage(
        [MetadataControl(control_type="date", name="task_386366760_date")],
        [prompt],
    )
    adapter = expected_adapter()
    form = await adapter.read_application(page)

    assert len(form.fields) == 1
    assert form.fields[0].kind == "unsupported"
    plan = ApplicationPlan(
        vacancy_id=int(VACANCY_ID),
        resume_file="resume.pdf",
        submission_allowed=True,
    )

    result = await adapter.fill_application(page, plan)

    assert not result.success
    assert result.unknown_questions == [prompt]
