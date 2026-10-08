from __future__ import annotations

import asyncio
from time import perf_counter

import pytest
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from backend.adapters.hh import locators
from backend.adapters.hh.adapter import HHAdapter


class _Locator:
    def __init__(self, *, count=0, visible=False, text="", inner_text_error=None, hangs=False):
        self._count = count
        self._visible = visible
        self._text = text
        self._inner_text_error = inner_text_error
        self._hangs = hangs
        self.cancelled = False

    @property
    def first(self):
        return self

    async def count(self):
        return self._count

    def nth(self, _index):
        return self

    async def is_visible(self):
        return self._visible

    async def get_attribute(self, _name):
        return None

    async def inner_text(self):
        if self._inner_text_error:
            raise self._inner_text_error
        if self._hangs:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise
        return self._text


class _Page:
    def __init__(self, *, dialog, body_text="", url="https://krasnoyarsk.hh.ru/vacancy/123"):
        self.dialog = dialog
        self.body_text = body_text
        self.url = url

    def locator(self, selector):
        if selector == locators.CAPTCHA_CHALLENGE:
            return _Locator()
        if selector == locators.CAPTCHA_DIALOG:
            return self.dialog
        if selector == "body":
            return _Locator(count=1, visible=True, text=self.body_text)
        if selector in {locators.VACANCY_TITLE, locators.DESCRIPTION, locators.VACANCY_LINK}:
            return _Locator()
        raise AssertionError(f"Unexpected selector: {selector}")


@pytest.mark.asyncio
async def test_disappearing_dialog_timeout_does_not_block_ordinary_page():
    dialog = _Locator(count=1, visible=True, hangs=True)
    page = _Page(dialog=dialog)
    started = perf_counter()

    blockers = await asyncio.wait_for(HHAdapter().detect_blockers(page), timeout=1)

    assert blockers == []
    assert dialog.cancelled
    assert perf_counter() - started < 1


@pytest.mark.asyncio
async def test_transient_dialog_timeout_keeps_body_and_path_captcha_fallbacks():
    cases = [
        ("Подтвердите, что вы не робот", "https://krasnoyarsk.hh.ru/vacancy/123"),
        ("", "https://krasnoyarsk.hh.ru/captcha/challenge"),
    ]
    for body_text, url in cases:
        page = _Page(
            dialog=_Locator(
                count=1,
                visible=True,
                inner_text_error=PlaywrightTimeoutError("dialog disappeared"),
            ),
            body_text=body_text,
            url=url,
        )

        blockers = await HHAdapter().detect_blockers(page)

        assert [blocker.kind for blocker in blockers] == ["captcha"]


@pytest.mark.asyncio
async def test_detached_dialog_probe_is_ignored_on_ordinary_page():
    page = _Page(
        dialog=_Locator(
            count=1,
            visible=True,
            inner_text_error=PlaywrightError("Element is not attached to the DOM"),
        )
    )

    assert await HHAdapter().detect_blockers(page) == []


@pytest.mark.asyncio
async def test_closed_browser_error_from_dialog_probe_is_not_suppressed():
    page = _Page(
        dialog=_Locator(
            count=1,
            visible=True,
            inner_text_error=PlaywrightError("Target page, context or browser has been closed"),
        )
    )

    with pytest.raises(PlaywrightError, match="browser has been closed"):
        await HHAdapter().detect_blockers(page)
