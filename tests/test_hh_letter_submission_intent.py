from __future__ import annotations

import pytest

from backend.adapters.base.errors import CaptchaRequired
from backend.adapters.base.protocol import FillResult, SubmissionResult
from backend.adapters.hh import forms, locators
from backend.adapters.hh.adapter import HHAdapter
from backend.schemas.domain import ApplicationPlan

LETTER = "Synthetic exact cover letter"


class _Locator:
    def __init__(
        self,
        *,
        count=0,
        visible=False,
        enabled=True,
        value="",
        on_click=None,
        mismatch=False,
    ):
        self._count = count
        self._visible = visible
        self._enabled = enabled
        self.value = value
        self._on_click = on_click
        self._mismatch = mismatch
        self.first = self

    async def count(self):
        return self._count

    async def is_visible(self):
        return self._visible

    async def is_enabled(self):
        return self._enabled

    async def fill(self, value):
        self.value = f"{value} mismatch" if self._mismatch else value

    async def input_value(self):
        return self.value

    async def click(self):
        if self._on_click:
            self._on_click()

    def locator(self, _selector):
        return self


class _Page:
    url = "https://krasnoyarsk.hh.ru/vacancy/123"

    def __init__(self, *, dialog=False, mismatch=False, ordinary_submit=False):
        self.clicks = 0
        self.dialog_visible = dialog
        self.input = _Locator(count=1, visible=True, mismatch=mismatch)
        self.dialog = _Locator(count=int(dialog), visible=dialog)
        self.ordinary_submit = ordinary_submit
        self.submit = _Locator(
            count=int(ordinary_submit),
            visible=ordinary_submit,
            on_click=lambda: setattr(self, "clicks", self.clicks + 1),
        )

    def locator(self, selector):
        if selector == locators.COVER_LETTER_INPUT:
            return self.input if not self.dialog_visible else _Locator()
        if selector == locators.COVER_LETTER_DIALOG:
            return self.dialog if self.dialog_visible else _Locator()
        if selector == locators.RESPONSE_SUBMIT:
            return self.submit
        if selector == locators.ALREADY_APPLIED:
            return _Locator()
        if selector == "body":
            return _BodyLocator()
        return _Locator()


class _BodyLocator:
    async def inner_text(self):
        return ""


def _plan(letter=LETTER):
    return ApplicationPlan(
        vacancy_id=123,
        resume_file="",
        submission_allowed=True,
        cover_letter=letter,
    )


def _make_fillable(adapter, monkeypatch):
    async def no_captcha(_page):
        return None

    async def fill_other_fields(_page, _plan):
        return FillResult(success=True)

    monkeypatch.setattr(adapter, "_ensure_no_captcha", no_captcha)
    monkeypatch.setattr(forms, "fill_fields", fill_other_fields)


@pytest.mark.asyncio
async def test_intent_hook_marks_only_after_exact_ordinary_fill_and_never_clicks(monkeypatch):
    adapter = HHAdapter()
    page = _Page(ordinary_submit=True)
    _make_fillable(adapter, monkeypatch)

    assert adapter.prepare_submission_progress().get("cover_letter_attempted", False) is False
    filled = await adapter.fill_application(page, _plan())
    assert filled.success
    assert adapter._hh_cover_letter_filled is True
    assert adapter.prepare_submission_progress()["cover_letter_attempted"] is True
    assert page.clicks == 0


@pytest.mark.asyncio
async def test_intent_hook_marks_separate_dialog_after_exact_fill_without_click(monkeypatch):
    adapter = HHAdapter()
    adapter._hh_cover_letter_dialog_pending = True
    adapter._hh_cover_letter_pending = True
    page = _Page(dialog=True)
    _make_fillable(adapter, monkeypatch)

    filled = await adapter.fill_application(page, _plan())
    assert filled.success
    assert adapter._hh_cover_letter_filled is True
    assert adapter.prepare_submission_progress()["cover_letter_attempted"] is True
    assert page.clicks == 0


@pytest.mark.asyncio
async def test_failed_readback_never_marks_letter_filled_or_attempted(monkeypatch):
    adapter = HHAdapter()
    page = _Page(mismatch=True)
    _make_fillable(adapter, monkeypatch)

    filled = await adapter.fill_application(page, _plan())
    progress = adapter.prepare_submission_progress()

    assert not filled.success
    assert adapter._hh_cover_letter_filled is False
    assert progress.get("cover_letter_attempted", False) is False
    assert page.clicks == 0


@pytest.mark.asyncio
async def test_unknown_ordinary_submission_is_not_clicked_twice(monkeypatch):
    adapter = HHAdapter()
    page = _Page(ordinary_submit=True)
    _make_fillable(adapter, monkeypatch)

    assert (await adapter.fill_application(page, _plan())).success

    async def unknown(_page, just_submitted=False):
        return SubmissionResult(status="unknown", message="unconfirmed")

    monkeypatch.setattr(adapter, "verify_submission", unknown)
    first = await adapter.submit_application(page)
    second = await adapter.submit_application(page)

    assert first.status == second.status == "unknown"
    assert page.clicks == 1
    assert adapter.get_submission_progress()["cover_letter_attempted"] is True


@pytest.mark.asyncio
async def test_open_application_clears_attempt_and_fill_before_early_captcha(monkeypatch):
    adapter = HHAdapter()
    adapter._hh_cover_letter_expected = True
    adapter._hh_cover_letter_filled = True
    adapter._hh_cover_letter_attempted = True
    adapter._hh_cover_letter_confirmed = True
    page = _Page()

    async def captcha(_page):
        raise CaptchaRequired("captcha")

    monkeypatch.setattr(adapter, "_ensure_no_captcha", captcha)

    with pytest.raises(CaptchaRequired):
        await adapter.open_application(page)

    progress = adapter.get_submission_progress()
    assert progress["cover_letter_confirmed"] is False
    assert progress.get("cover_letter_attempted", False) is False
    assert adapter._hh_cover_letter_expected is False
    assert adapter._hh_cover_letter_filled is False
    assert page.clicks == 0


@pytest.mark.asyncio
async def test_new_resume_attempt_resets_letter_fill_and_attempt_intent(monkeypatch):
    adapter = HHAdapter()
    adapter._hh_cover_letter_attempted = True
    adapter._hh_cover_letter_filled = True
    page = _Page()
    _make_fillable(adapter, monkeypatch)

    async def confirmed_cv(_page):
        return SubmissionResult(status="submitted", message="CV confirmed")

    async def read_form(_page, plan):
        return type("Form", (), {"requires_cover_letter": True})()

    monkeypatch.setattr(adapter, "verify_cv_submission", confirmed_cv)
    monkeypatch.setattr(adapter, "prepare_application", read_form)
    await adapter.resume_application(
        page, _plan(), cv_confirmed=True, cover_letter_pending=True
    )

    assert adapter._hh_cover_letter_expected is True
    assert adapter._hh_cover_letter_filled is False
    assert adapter.get_submission_progress().get("cover_letter_attempted", False) is False


@pytest.mark.asyncio
async def test_resume_clears_old_intent_before_early_captcha(monkeypatch):
    adapter = HHAdapter()
    adapter._hh_cover_letter_expected = True
    adapter._hh_cover_letter_filled = True
    adapter._hh_cover_letter_attempted = True
    page = _Page()

    async def captcha(_page):
        raise CaptchaRequired("captcha")

    monkeypatch.setattr(adapter, "_ensure_no_captcha", captcha)

    with pytest.raises(CaptchaRequired):
        await adapter.resume_application(
            page, _plan(), cv_confirmed=True, cover_letter_pending=True
        )

    assert adapter._hh_cover_letter_expected is True
    assert adapter._hh_cover_letter_filled is False
    assert adapter._hh_cover_letter_attempted is False
