from __future__ import annotations

import pytest

from backend.adapters.base.errors import CaptchaRequired
from backend.adapters.hh import locators
from backend.adapters.hh.adapter import HHAdapter
from backend.orchestrator.recovery import AuthenticationPending


class _Locator:
    def __init__(self, *, count=0, visible=False):
        self._count = count
        self._visible = visible
        self.click_count = 0

    @property
    def first(self):
        return self

    async def count(self):
        return self._count

    async def is_visible(self):
        return self._visible

    async def inner_text(self):
        return ""

    async def click(self):
        self.click_count += 1


class _Page:
    url = "https://krasnoyarsk.hh.ru/vacancy/123"

    def __init__(self, *, cv_confirmed=True, attach=False, dialog=False):
        self.cv = _Locator(count=int(cv_confirmed))
        self.attach = _Locator(count=int(attach), visible=attach)
        self.dialog = _Locator(count=int(dialog), visible=dialog)
        self.other = _Locator()
        self.body = _Locator(count=1)

    def locator(self, selector):
        if selector == locators.ALREADY_APPLIED:
            return self.cv
        if selector == locators.ATTACH_COVER_LETTER:
            return self.attach
        if selector == locators.COVER_LETTER_DIALOG:
            return self.dialog
        if selector == "body":
            return self.body
        return self.other

@pytest.mark.asyncio
async def test_cv_topic_alone_leaves_expected_letter_unknown_and_unsafe():
    adapter = HHAdapter()
    page = _Page(cv_confirmed=True)

    progress = await adapter.reconcile_submission_progress(
        page, letter_expected=True
    )

    assert progress == {
        "cv_confirmed": True,
        "cover_letter_pending": True,
        "cover_letter_confirmed": False,
        "letter_recovery_safe": False,
    }
    assert page.attach.click_count == 0
    assert page.dialog.click_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("visible_action", ["attach", "dialog"])
async def test_visible_letter_action_makes_recovery_safe(visible_action):
    adapter = HHAdapter()
    page = _Page(
        cv_confirmed=True,
        attach=visible_action == "attach",
        dialog=visible_action == "dialog",
    )

    progress = await adapter.reconcile_submission_progress(
        page, letter_expected=True
    )

    assert progress["cv_confirmed"] is True
    assert progress["cover_letter_pending"] is True
    assert progress["cover_letter_confirmed"] is False
    assert progress["letter_recovery_safe"] is True
    assert page.attach.click_count == 0
    assert page.dialog.click_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(("attach", "safe"), [(False, False), (True, True)])
async def test_previous_vacancy_letter_flag_is_ignored_during_reconciliation(
    attach, safe
):
    adapter = HHAdapter()
    adapter._hh_cover_letter_confirmed = True
    page = _Page(cv_confirmed=True, attach=attach)
    page.url = "https://krasnoyarsk.hh.ru/vacancy/456"

    progress = await adapter.reconcile_submission_progress(
        page, letter_expected=True
    )

    assert progress == {
        "cv_confirmed": True,
        "cover_letter_pending": True,
        "cover_letter_confirmed": False,
        "letter_recovery_safe": safe,
    }
    assert page.attach.click_count == 0
    assert page.dialog.click_count == 0


@pytest.mark.asyncio
async def test_unexpected_letter_does_not_create_pending_or_confirmation():
    adapter = HHAdapter()
    page = _Page(cv_confirmed=True, attach=True, dialog=True)

    progress = await adapter.reconcile_submission_progress(
        page, letter_expected=False
    )

    assert progress == {
        "cv_confirmed": True,
        "cover_letter_pending": False,
        "cover_letter_confirmed": False,
        "letter_recovery_safe": False,
    }
    assert page.attach.click_count == 0
    assert page.dialog.click_count == 0


@pytest.mark.asyncio
async def test_unknown_cv_does_not_reconcile_letter_controls():
    adapter = HHAdapter()
    page = _Page(cv_confirmed=False, attach=True, dialog=True)

    progress = await adapter.reconcile_submission_progress(
        page, letter_expected=True
    )

    assert progress == {
        "cv_confirmed": False,
        "cover_letter_pending": False,
        "cover_letter_confirmed": False,
        "letter_recovery_safe": False,
    }
    assert page.attach.click_count == 0
    assert page.dialog.click_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error", [CaptchaRequired("captcha"), AuthenticationPending("login")]
)
async def test_captcha_and_authentication_errors_propagate(monkeypatch, error):
    adapter = HHAdapter()
    page = _Page()

    async def reject(_page):
        raise error

    monkeypatch.setattr(adapter, "_ensure_no_captcha", reject)

    with pytest.raises(type(error)):
        await adapter.reconcile_submission_progress(page, letter_expected=True)


@pytest.mark.asyncio
async def test_foreign_domain_is_rejected():
    adapter = HHAdapter()
    page = _Page()
    page.url = "https://example.com/vacancy/123"

    with pytest.raises(ValueError, match="разрешённых доменов"):
        await adapter.reconcile_submission_progress(page, letter_expected=True)


@pytest.mark.asyncio
async def test_open_application_resets_old_progress_before_early_captcha(monkeypatch):
    adapter = HHAdapter()
    adapter._application_attempt_clicked = True
    adapter._hh_cv_submission_confirmed = True
    adapter._hh_cover_letter_pending = True
    adapter._hh_cover_letter_confirmed = True
    adapter._hh_ordinary_cover_letter_filled = True
    adapter._hh_ordinary_cover_letter_submit_clicked = True
    adapter._hh_cover_letter_dialog_pending = True
    adapter._hh_cover_letter_submit_clicked = True
    adapter._hh_cover_letter_field_diagnostic = {"category": "old-vacancy"}
    page = _Page()

    async def captcha(_page):
        raise CaptchaRequired("captcha")

    monkeypatch.setattr(adapter, "_ensure_no_captcha", captcha)

    with pytest.raises(CaptchaRequired):
        await adapter.open_application(page)

    assert adapter.get_submission_progress() == {
        "cv_confirmed": False,
        "cover_letter_pending": False,
        "cover_letter_confirmed": False,
        "cover_letter_diagnostic": None,
    }
    assert adapter._application_attempt_clicked is False
    assert adapter._hh_ordinary_cover_letter_filled is False
    assert adapter._hh_ordinary_cover_letter_submit_clicked is False
    assert adapter._hh_cover_letter_dialog_pending is False
    assert adapter._hh_cover_letter_submit_clicked is False
    assert page.other.click_count == 0
