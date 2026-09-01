from __future__ import annotations

import pytest

from backend.adapters.zarplata import locators
from backend.adapters.zarplata.adapter import ZarplataAdapter
from backend.schemas.domain import ApplicationPlan


class FakeLocator:
    def __init__(self, page, selector):
        self.page = page
        self.selector = selector
        self.first = self

    async def count(self):
        return int(self.page.visible.get(self.selector, False))

    async def click(self):
        self.page.clicks.append(self.selector)
        if self.selector == locators.RESPONSE_BUTTON:
            self.page.visible[locators.RESPONSE_BUTTON] = False
            if self.page.one_click_result == "topic":
                self.page.visible[locators.ALREADY_APPLIED] = True
            elif self.page.one_click_result == "success":
                self.page.visible[locators.SUBMISSION_CONFIRMED] = True
        elif self.selector == locators.RESPONSE_SUBMIT:
            self.page.visible[locators.RESPONSE_SUBMIT] = False
            self.page.visible[locators.SUBMISSION_CONFIRMED] = True

    async def fill(self, value):
        self.page.filled.append((self.selector, value))

    async def all_text_contents(self):
        return []

    async def inner_text(self):
        return self.page.body_text if self.selector == "body" else ""


class FakePage:
    url = "https://zarplata.ru/vacancy/1"

    def __init__(self, visible=None, one_click_result=None, body_text=""):
        self.visible = visible or {}
        self.one_click_result = one_click_result
        self.body_text = body_text
        self.clicks = []
        self.filled = []

    def locator(self, selector):
        return FakeLocator(self, selector)

    async def wait_for_timeout(self, _ms):
        return None


@pytest.mark.asyncio
async def test_existing_response_without_click_is_already_applied():
    page = FakePage({locators.ALREADY_APPLIED: True})

    result = await ZarplataAdapter().submit_application(page)

    assert result.status == "already_applied"


@pytest.mark.asyncio
async def test_one_click_response_is_submitted_when_topic_link_appears():
    page = FakePage({locators.RESPONSE_BUTTON: True}, one_click_result="topic")
    adapter = ZarplataAdapter()

    await adapter.open_application(page)
    result = await adapter.submit_application(page)

    assert result.status == "submitted"
    assert page.clicks == [locators.RESPONSE_BUTTON]


@pytest.mark.asyncio
async def test_one_click_response_is_submitted_when_only_success_marker_appears():
    page = FakePage({locators.RESPONSE_BUTTON: True}, one_click_result="success")
    adapter = ZarplataAdapter()

    await adapter.open_application(page)
    result = await adapter.submit_application(page)

    assert result.status == "submitted"


@pytest.mark.asyncio
async def test_popup_submit_is_submitted_once():
    page = FakePage(
        {locators.RESPONSE_BUTTON: True, locators.RESPONSE_SUBMIT: True}
    )
    adapter = ZarplataAdapter()

    await adapter.open_application(page)
    result = await adapter.submit_application(page)

    assert result.status == "submitted"
    assert page.clicks == [locators.RESPONSE_BUTTON, locators.RESPONSE_SUBMIT]


@pytest.mark.asyncio
async def test_clicked_attempt_without_confirmation_is_unknown():
    page = FakePage({locators.RESPONSE_BUTTON: True})
    adapter = ZarplataAdapter()

    await adapter.open_application(page)
    result = await adapter.submit_application(page)

    assert result.status == "unknown"


class Control:
    def __init__(self, *, name="", data_qa="", placeholder="", visible=True):
        self.name = name
        self.data_qa = data_qa
        self.placeholder = placeholder
        self.visible = visible

    async def is_visible(self):
        return self.visible

    async def get_attribute(self, name):
        return {
            "type": "text",
            "name": self.name,
            "data-qa": self.data_qa,
            "placeholder": self.placeholder,
            "aria-label": "",
        }.get(name)


class ControlLocator:
    def __init__(self, controls):
        self.controls = controls

    async def count(self):
        return len(self.controls)

    def nth(self, index):
        return self.controls[index]


class EmptyTextLocator:
    async def all_text_contents(self):
        return []


class QuestionPage(FakePage):
    def __init__(self, controls):
        super().__init__()
        self.controls = controls

    def locator(self, selector):
        if selector == locators.APPLICATION_CONTROL:
            return ControlLocator(self.controls)
        if selector in {locators.TASK_QUESTION, locators.APPLICATION_QUESTION}:
            return EmptyTextLocator()
        return super().locator(selector)


@pytest.mark.asyncio
async def test_unknown_employer_question_blocks_fill():
    page = QuestionPage(
        [Control(name="task_1", placeholder="Почему вы хотите у нас работать?")]
    )
    plan = ApplicationPlan(
        vacancy_id="1", resume_file="resume.pdf", submission_allowed=True
    )

    result = await ZarplataAdapter().fill_application(page, plan)

    assert not result.success
    assert result.unknown_questions == ["Почему вы хотите у нас работать?"]


@pytest.mark.asyncio
async def test_cover_letter_control_is_not_an_unknown_question():
    page = QuestionPage(
        [
            Control(
                name="cover_letter",
                data_qa="vacancy-response-popup-form-letter-input",
            )
        ]
    )
    plan = ApplicationPlan(
        vacancy_id="1", resume_file="resume.pdf", submission_allowed=True
    )

    result = await ZarplataAdapter().fill_application(page, plan)

    assert result.success
    assert result.unknown_questions == []


@pytest.mark.asyncio
async def test_captcha_is_reported_for_manual_handling():
    page = FakePage(body_text="Подтвердите, что вы не робот")

    blockers = await ZarplataAdapter().detect_blockers(page)

    assert len(blockers) == 1
    assert blockers[0].kind == "captcha"


@pytest.mark.asyncio
async def test_no_captcha_has_no_blockers():
    assert await ZarplataAdapter().detect_blockers(FakePage()) == []
