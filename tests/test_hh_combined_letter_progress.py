from __future__ import annotations

import pytest

from backend.adapters.base.protocol import ApplicationForm, FillResult, SubmissionResult
from backend.adapters.hh import forms, locators
from backend.adapters.hh.adapter import HHAdapter
from backend.schemas.domain import ApplicationPlan

LETTER = "Здравствуйте! У меня есть релевантный опыт."


class _Locator:
    def __init__(self, *, count=0, visible=False, enabled=True, value="", on_click=None):
        self._count = count
        self._visible = visible
        self._enabled = enabled
        self.value = value
        self._on_click = on_click

    @property
    def first(self):
        return self

    async def count(self):
        return self._count

    async def is_visible(self):
        return self._visible

    async def is_enabled(self):
        return self._enabled

    async def fill(self, value):
        self.value = value

    async def input_value(self):
        return self.value

    async def click(self):
        if self._on_click:
            self._on_click()

    def locator(self, _selector):
        return self


class _Page:
    url = "https://krasnoyarsk.hh.ru/vacancy/123"

    def __init__(self, *, previously_applied=False, pending_dialog=False, letter_enabled=True):
        self.previously_applied = previously_applied
        self.submitted = False
        self.pending_dialog = pending_dialog
        self.input = _Locator(count=1, visible=True, enabled=letter_enabled)
        self.dialog = _Locator(count=int(pending_dialog), visible=pending_dialog)
        self.submit = _Locator(
            count=1,
            visible=True,
            on_click=lambda: setattr(self, "submitted", True),
        )

    def locator(self, selector):
        if selector == locators.COVER_LETTER_INPUT:
            return self.input if not self.pending_dialog else _Locator()
        if selector == locators.COVER_LETTER_DIALOG:
            if self.pending_dialog:
                return self.dialog
            return _Locator()
        if selector == locators.RESPONSE_SUBMIT:
            return _Locator(
                count=int(not self.submitted and not self.previously_applied),
                visible=not self.submitted and not self.previously_applied,
                on_click=lambda: setattr(self, "submitted", True),
            )
        if selector == locators.ALREADY_APPLIED:
            return _Locator(count=int(self.previously_applied or self.submitted))
        if selector == locators.COVER_LETTER_TOGGLE:
            return _Locator()
        return _Locator()

    def get_by_text(self, *_args, **_kwargs):
        return _Locator()


def _plan(letter=LETTER):
    return ApplicationPlan(
        vacancy_id=1,
        resume_file="",
        submission_allowed=True,
        cover_letter=letter,
    )


def _make_testable(adapter, monkeypatch):
    async def no_captcha(_page):
        return None

    async def no_other_fields(_page, _plan):
        return FillResult(success=True)

    monkeypatch.setattr(adapter, "_ensure_no_captcha", no_captcha)
    monkeypatch.setattr(forms, "fill_fields", no_other_fields)


@pytest.mark.asyncio
async def test_ordinary_exact_letter_is_confirmed_only_after_site_submission(monkeypatch):
    adapter = HHAdapter()
    page = _Page()
    _make_testable(adapter, monkeypatch)

    filled = await adapter.fill_application(page, _plan())
    assert filled.success
    assert page.input.value == LETTER
    assert not adapter.get_submission_progress()["cover_letter_confirmed"]
    result = await adapter.submit_application(page)

    assert result.status == "submitted"
    assert adapter.get_submission_progress()["cv_confirmed"]
    assert adapter.get_submission_progress()["cover_letter_confirmed"]


@pytest.mark.asyncio
async def test_unknown_submission_does_not_confirm_ordinary_letter(monkeypatch):
    adapter = HHAdapter()
    page = _Page()
    _make_testable(adapter, monkeypatch)
    await adapter.fill_application(page, _plan())

    async def unknown(_page, just_submitted=False):
        return SubmissionResult(status="unknown", message="unconfirmed")

    monkeypatch.setattr(adapter, "verify_submission", unknown)
    result = await adapter.submit_application(page)

    assert result.status == "unknown"
    assert not adapter.get_submission_progress()["cover_letter_confirmed"]


@pytest.mark.asyncio
async def test_failed_letter_fill_does_not_mark_ordinary_letter_ready(monkeypatch):
    adapter = HHAdapter()
    page = _Page(letter_enabled=False)
    _make_testable(adapter, monkeypatch)

    filled = await adapter.fill_application(page, _plan())

    assert not filled.success
    assert not adapter._hh_ordinary_cover_letter_filled
    assert not adapter.get_submission_progress()["cover_letter_confirmed"]


@pytest.mark.asyncio
async def test_no_letter_or_preexisting_application_does_not_confirm_letter(monkeypatch):
    adapter = HHAdapter()
    page = _Page(previously_applied=True)
    _make_testable(adapter, monkeypatch)

    await adapter.fill_application(page, _plan(letter=""))
    result = await adapter.submit_application(page)

    assert result.status == "already_applied"
    assert not adapter.get_submission_progress()["cover_letter_confirmed"]


@pytest.mark.asyncio
async def test_separate_pending_dialog_is_not_counted_as_ordinary_letter(monkeypatch):
    adapter = HHAdapter()
    adapter._hh_cover_letter_dialog_pending = True
    adapter._hh_cover_letter_pending = True
    page = _Page(pending_dialog=True)
    _make_testable(adapter, monkeypatch)

    filled = await adapter.fill_application(page, _plan())

    assert filled.success
    assert page.dialog.value == LETTER
    assert not adapter.get_submission_progress()["cover_letter_confirmed"]


@pytest.mark.asyncio
async def test_opening_new_attempt_resets_letter_progress(monkeypatch):
    adapter = HHAdapter()
    adapter._hh_ordinary_cover_letter_filled = True
    adapter._hh_ordinary_cover_letter_submit_clicked = True
    adapter._hh_cover_letter_confirmed = True
    page = _Page(previously_applied=True)
    _make_testable(adapter, monkeypatch)

    async def empty_form(_page):
        return ApplicationForm()

    monkeypatch.setattr(adapter, "read_application", empty_form)
    await adapter.open_application(page)

    assert not adapter.get_submission_progress()["cover_letter_confirmed"]
    assert not adapter._hh_ordinary_cover_letter_filled
    assert not adapter._hh_ordinary_cover_letter_submit_clicked
