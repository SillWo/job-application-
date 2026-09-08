from __future__ import annotations

import pytest

from backend.adapters.hh import locators
from backend.adapters.hh.adapter import HHAdapter


class FakeLocator:
    def __init__(self, page, selector):
        self.page = page
        self.selector = selector
        self.first = self

    async def count(self):
        return int(self.page.visible.get(self.selector, False))

    async def is_visible(self):
        return bool(await self.count())

    async def click(self):
        self.page.clicks.append(self.selector)
        if self.selector == locators.RESPONSE_BUTTON:
            self.page.visible[locators.RESPONSE_BUTTON] = False
            self.page.visible[locators.ALREADY_APPLIED] = True
        elif self.selector == locators.RESPONSE_SUBMIT:
            self.page.visible[locators.RESPONSE_SUBMIT] = False
            self.page.visible[locators.SUBMISSION_CONFIRMED] = True

    async def all_text_contents(self):
        return []

    async def inner_text(self):
        return ""


class FakePage:
    url = "https://hh.ru/vacancy/1"

    def __init__(self, visible):
        self.visible = visible
        self.clicks = []

    def locator(self, selector):
        return FakeLocator(self, selector)

    def get_by_text(self, text, **kwargs):
        return FakeLocator(self, text)

    async def wait_for_timeout(self, _ms):
        return None


@pytest.mark.asyncio
async def test_existing_response_without_click_is_already_applied():
    page = FakePage({locators.ALREADY_APPLIED: True})

    result = await HHAdapter().submit_application(page)

    assert result.status == "already_applied"


@pytest.mark.asyncio
async def test_one_click_response_is_submitted_after_topic_link_appears():
    page = FakePage({locators.RESPONSE_BUTTON: True})
    adapter = HHAdapter()

    await adapter.open_application(page)
    result = await adapter.submit_application(page)

    assert result.status == "submitted"
    assert page.clicks == [locators.RESPONSE_BUTTON]


@pytest.mark.asyncio
async def test_popup_submit_is_submitted():
    page = FakePage({locators.RESPONSE_BUTTON: True, locators.RESPONSE_SUBMIT: True})
    adapter = HHAdapter()

    await adapter.open_application(page)
    result = await adapter.submit_application(page)

    assert result.status == "submitted"
    assert page.clicks == [locators.RESPONSE_BUTTON, locators.RESPONSE_SUBMIT]


@pytest.mark.asyncio
async def test_clicked_attempt_without_confirmation_is_unknown():
    page = FakePage({locators.RESPONSE_BUTTON: True})
    adapter = HHAdapter()

    await adapter.open_application(page)
    page.visible[locators.RESPONSE_BUTTON] = False
    page.visible[locators.ALREADY_APPLIED] = False
    result = await adapter.submit_application(page)

    assert result.status == "unknown"
