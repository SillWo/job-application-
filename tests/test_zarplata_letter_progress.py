from __future__ import annotations

import pytest

from backend.adapters.base.errors import CaptchaRequired, JobDescriptionUnavailable
from backend.adapters.zarplata import locators
from backend.adapters.zarplata.adapter import ZarplataAdapter
from backend.orchestrator.recovery import AuthenticationPending
from backend.schemas.domain import ApplicationPlan

LETTER = "Synthetic cover letter"


class _Locator:
    def __init__(self, page, selector):
        self.page = page
        self.selector = selector
        self.first = self

    async def count(self):
        return int(
            self.selector not in self.page.absent
            and self.selector in self.page.present
        )

    async def is_visible(self):
        return bool(self.page.visible.get(self.selector, False))

    async def is_enabled(self):
        return bool(self.page.enabled.get(self.selector, True))

    async def click(self):
        self.page.clicks.append(self.selector)
        if self.selector == locators.RESPONSE_BUTTON:
            self.page.visible[locators.RESPONSE_BUTTON] = False
            self.page.absent.add(locators.RESPONSE_BUTTON)
            if self.page.ordinary_form:
                self.page.visible[locators.RESPONSE_SUBMIT] = True
                self.page.present.add(locators.RESPONSE_SUBMIT)
            else:
                self.page.visible[locators.ALREADY_APPLIED] = True
                self.page.present.add(locators.ALREADY_APPLIED)
            if self.page.captcha_after_response:
                self.page.body_text = "Подтвердите, что вы не робот"
        elif self.selector == locators.RESPONSE_SUBMIT:
            self.page.visible[locators.RESPONSE_SUBMIT] = False
            self.page.absent.add(locators.RESPONSE_SUBMIT)
            if self.page.ordinary_confirm:
                self.page.visible[locators.SUBMISSION_CONFIRMED] = True
                self.page.present.add(locators.SUBMISSION_CONFIRMED)
        elif self.selector == locators.COVER_LETTER_SUBMIT:
            self.page.letter_clicked_at = self.page.elapsed_ms
            if self.page.raise_on_letter_click:
                raise RuntimeError("ambiguous synthetic click")
            if self.page.url_after_letter:
                self.page.url = self.page.url_after_letter
            if self.page.captcha_after_letter:
                self.page.body_text = "Подтвердите, что вы не робот"
            if self.page.letter_hide_on_click:
                self.page.visible[locators.COVER_LETTER_INPUT] = False
                self.page.visible[locators.COVER_LETTER_SUBMIT] = False
                self.page.absent.update(
                    {locators.COVER_LETTER_INPUT, locators.COVER_LETTER_SUBMIT}
                )
                self.page.letter_submitted = True

    async def fill(self, value):
        self.page.values[self.selector] = f"{value} changed" if self.page.mismatch_fill else value

    async def input_value(self):
        return self.page.values.get(self.selector, "")

    async def all_text_contents(self):
        return []

    async def inner_text(self):
        return self.page.body_text if self.selector == "body" else ""

    async def get_attribute(self, _name):
        return "text"

    def nth(self, _index):
        return self


class _Page:
    url = "https://zarplata.ru/vacancy/1"

    def __init__(
        self,
        *,
        visible=None,
        enabled=None,
        ordinary_form=False,
        ordinary_confirm=True,
        letter_hide_on_click=True,
        delayed_letter_ms=None,
        letter_confirm_after_ms=None,
        captcha_after_response=False,
        captcha_after_letter=False,
        url_after_letter=None,
        raise_on_letter_click=False,
        mismatch_fill=False,
    ):
        self.visible = dict(visible or {})
        self.present = set(self.visible)
        self.absent = set()
        self.enabled = dict(enabled or {})
        self.values = {}
        self.clicks = []
        self.body_text = ""
        self.letter_submitted = False
        self.ordinary_form = ordinary_form
        self.ordinary_confirm = ordinary_confirm
        self.letter_hide_on_click = letter_hide_on_click
        self.delayed_letter_ms = delayed_letter_ms
        self.letter_confirm_after_ms = letter_confirm_after_ms
        self.captcha_after_response = captcha_after_response
        self.captcha_after_letter = captcha_after_letter
        self.url_after_letter = url_after_letter
        self.raise_on_letter_click = raise_on_letter_click
        self.mismatch_fill = mismatch_fill
        self.elapsed_ms = 0
        self.letter_clicked_at = None

    def locator(self, selector):
        return _Locator(self, selector)

    async def wait_for_timeout(self, _ms):
        self.elapsed_ms += _ms
        if (
            self.delayed_letter_ms is not None
            and self.elapsed_ms >= self.delayed_letter_ms
        ):
            self.visible[locators.COVER_LETTER_INPUT] = True
            self.visible[locators.COVER_LETTER_SUBMIT] = True
            self.present.update(
                {locators.COVER_LETTER_INPUT, locators.COVER_LETTER_SUBMIT}
            )
            self.absent.difference_update(
                {locators.COVER_LETTER_INPUT, locators.COVER_LETTER_SUBMIT}
            )
        if (
            self.letter_clicked_at is not None
            and self.letter_confirm_after_ms is not None
            and self.elapsed_ms - self.letter_clicked_at >= self.letter_confirm_after_ms
        ):
            self.visible[locators.COVER_LETTER_INPUT] = False
            self.visible[locators.COVER_LETTER_SUBMIT] = False
            self.absent.update(
                {locators.COVER_LETTER_INPUT, locators.COVER_LETTER_SUBMIT}
            )


def _plan(letter=LETTER):
    return ApplicationPlan(
        vacancy_id=1,
        resume_file="",
        submission_allowed=True,
        cover_letter=letter,
    )


@pytest.mark.asyncio
async def test_cv_topic_does_not_make_missing_expected_letter_a_success():
    page = _Page(visible={locators.RESPONSE_BUTTON: True})
    adapter = ZarplataAdapter()
    adapter._submission_timeout_ms = 0

    await adapter.open_application(page)
    filled = await adapter.fill_application(page, _plan())
    result = await adapter.submit_application(page)

    assert not filled.success
    assert result.status != "submitted"
    progress = adapter.get_submission_progress()
    assert progress["cv_confirmed"] is True
    assert progress["cover_letter_pending"] is True
    assert progress["cover_letter_confirmed"] is False


@pytest.mark.asyncio
async def test_visible_inline_letter_is_filled_and_submitted_without_cv_resubmit():
    page = _Page(
        visible={
            locators.ALREADY_APPLIED: True,
            locators.COVER_LETTER_INPUT: True,
            locators.COVER_LETTER_SUBMIT: True,
        }
    )
    adapter = ZarplataAdapter()
    adapter._submission_timeout_ms = 0
    adapter._letter_readiness_timeout_ms = 0

    await adapter.open_application(page)
    filled = await adapter.fill_application(page, _plan())
    result = await adapter.submit_application(page)

    assert filled.success
    assert page.values[locators.COVER_LETTER_INPUT] == LETTER
    assert result.status == "submitted"
    assert page.clicks == [locators.COVER_LETTER_SUBMIT]
    assert adapter.get_submission_progress()["cover_letter_confirmed"] is True


@pytest.mark.asyncio
async def test_ordinary_popup_confirms_letter_only_after_cv_submit_succeeds():
    page = _Page(
        visible={
            locators.RESPONSE_BUTTON: True,
            locators.COVER_LETTER_INPUT: True,
            locators.RESPONSE_SUBMIT: True,
        },
        ordinary_form=True,
    )
    adapter = ZarplataAdapter()
    adapter._submission_timeout_ms = 0
    adapter._letter_readiness_timeout_ms = 0

    await adapter.open_application(page)
    filled = await adapter.fill_application(page, _plan())
    result = await adapter.submit_application(page)

    assert filled.success
    assert result.status == "submitted"
    assert page.clicks == [locators.RESPONSE_BUTTON, locators.RESPONSE_SUBMIT]
    assert adapter.get_submission_progress()["cover_letter_confirmed"] is True


@pytest.mark.asyncio
async def test_late_letter_field_is_waited_for_within_readiness_bound():
    page = _Page(
        visible={locators.ALREADY_APPLIED: True},
        delayed_letter_ms=200,
    )
    adapter = ZarplataAdapter()
    adapter._letter_readiness_timeout_ms = 1_000

    form = await adapter.prepare_application(page, _plan())
    filled = await adapter.fill_application(page, _plan())

    assert form.requires_cover_letter
    assert filled.success
    assert page.values[locators.COVER_LETTER_INPUT] == LETTER


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("visible", "enabled", "category"),
    [(False, True, "hidden"), (True, False, "disabled")],
)
async def test_hidden_or_disabled_letter_field_cannot_be_reported_filled(
    visible, enabled, category
):
    page = _Page(
        visible={
            locators.ALREADY_APPLIED: True,
            locators.COVER_LETTER_INPUT: True,
        },
        enabled={locators.COVER_LETTER_INPUT: enabled},
    )
    page.visible[locators.COVER_LETTER_INPUT] = visible
    adapter = ZarplataAdapter()
    adapter._letter_readiness_timeout_ms = 0

    result = await adapter.fill_application(page, _plan())

    assert not result.success
    assert result.unknown_questions == ["Сопроводительное письмо"]
    assert adapter.get_submission_progress()["cover_letter_diagnostic"]["category"] == category


@pytest.mark.asyncio
async def test_exact_letter_readback_is_required_before_submit():
    page = _Page(
        visible={
            locators.ALREADY_APPLIED: True,
            locators.COVER_LETTER_INPUT: True,
            locators.COVER_LETTER_SUBMIT: True,
        },
        mismatch_fill=True,
    )
    adapter = ZarplataAdapter()
    adapter._letter_readiness_timeout_ms = 0

    result = await adapter.fill_application(page, _plan())
    submitted = await adapter.submit_application(page)

    assert not result.success
    assert submitted.status == "needs_input"
    assert page.clicks == []
    assert adapter.get_submission_progress()["cover_letter_confirmed"] is False


@pytest.mark.asyncio
async def test_letter_confirmation_waits_for_field_and_button_to_disappear():
    page = _Page(
        visible={
            locators.ALREADY_APPLIED: True,
            locators.COVER_LETTER_INPUT: True,
            locators.COVER_LETTER_SUBMIT: True,
        },
        letter_hide_on_click=False,
        letter_confirm_after_ms=600,
    )
    adapter = ZarplataAdapter()
    adapter._submission_timeout_ms = 1_000
    adapter._letter_readiness_timeout_ms = 0

    await adapter.fill_application(page, _plan())
    result = await adapter.submit_application(page)

    assert result.status == "submitted"
    assert adapter.get_submission_progress()["cover_letter_confirmed"] is True
    assert page.clicks == [locators.COVER_LETTER_SUBMIT]


@pytest.mark.asyncio
async def test_persistent_cv_topic_and_letter_form_never_confirm_letter():
    page = _Page(
        visible={
            locators.ALREADY_APPLIED: True,
            locators.COVER_LETTER_INPUT: True,
            locators.COVER_LETTER_SUBMIT: True,
        },
        letter_hide_on_click=False,
        letter_confirm_after_ms=None,
    )
    adapter = ZarplataAdapter()
    adapter._submission_timeout_ms = 0
    adapter._letter_readiness_timeout_ms = 0

    await adapter.fill_application(page, _plan())
    result = await adapter.submit_application(page)

    assert result.status == "unknown"
    assert adapter.get_submission_progress()["cv_confirmed"] is True
    assert adapter.get_submission_progress()["cover_letter_pending"] is True
    assert adapter.get_submission_progress()["cover_letter_confirmed"] is False
    assert page.visible[locators.ALREADY_APPLIED] is True


@pytest.mark.asyncio
async def test_repeated_submit_never_reclicks_ambiguous_letter_submission():
    page = _Page(
        visible={
            locators.ALREADY_APPLIED: True,
            locators.COVER_LETTER_INPUT: True,
            locators.COVER_LETTER_SUBMIT: True,
        },
        letter_hide_on_click=False,
    )
    adapter = ZarplataAdapter()
    adapter._submission_timeout_ms = 0
    adapter._letter_readiness_timeout_ms = 0

    assert (await adapter.fill_application(page, _plan())).success
    first = await adapter.submit_application(page)
    second = await adapter.submit_application(page)

    assert first.status == "unknown"
    assert second.status == "unknown"
    assert page.clicks == [locators.COVER_LETTER_SUBMIT]
    assert adapter.get_submission_progress()["cover_letter_confirmed"] is False
    assert adapter.get_submission_progress()["cover_letter_attempted"] is True


@pytest.mark.asyncio
async def test_progress_intent_is_persistable_before_any_letter_click():
    page = _Page(
        visible={
            locators.ALREADY_APPLIED: True,
            locators.COVER_LETTER_INPUT: True,
            locators.COVER_LETTER_SUBMIT: True,
        }
    )
    adapter = ZarplataAdapter()
    adapter._letter_readiness_timeout_ms = 0

    assert (await adapter.fill_application(page, _plan())).success
    progress = adapter.prepare_submission_progress()

    assert progress["cover_letter_attempted"] is True
    assert progress["cover_letter_confirmed"] is False
    assert page.clicks == []


@pytest.mark.asyncio
async def test_unverified_letter_value_does_not_mark_submission_attempted():
    page = _Page(
        visible={
            locators.ALREADY_APPLIED: True,
            locators.COVER_LETTER_INPUT: True,
            locators.COVER_LETTER_SUBMIT: True,
        },
        mismatch_fill=True,
    )
    adapter = ZarplataAdapter()
    adapter._letter_readiness_timeout_ms = 0

    filled = await adapter.fill_application(page, _plan())
    progress = adapter.prepare_submission_progress()

    assert not filled.success
    assert progress["cover_letter_attempted"] is False
    assert page.clicks == []


@pytest.mark.asyncio
async def test_ambiguous_letter_click_is_recorded_and_never_repeated():
    page = _Page(
        visible={
            locators.ALREADY_APPLIED: True,
            locators.COVER_LETTER_INPUT: True,
            locators.COVER_LETTER_SUBMIT: True,
        },
        raise_on_letter_click=True,
    )
    adapter = ZarplataAdapter()
    adapter._submission_timeout_ms = 0
    adapter._letter_readiness_timeout_ms = 0

    assert (await adapter.fill_application(page, _plan())).success
    with pytest.raises(RuntimeError, match="ambiguous synthetic click"):
        await adapter.submit_application(page)
    result = await adapter.submit_application(page)

    assert result.status == "unknown"
    assert page.clicks == [locators.COVER_LETTER_SUBMIT]
    assert adapter.get_submission_progress()["cover_letter_attempted"] is True


@pytest.mark.asyncio
async def test_restart_resumes_letter_without_clicking_cv_response():
    page = _Page(
        visible={
            locators.ALREADY_APPLIED: True,
            locators.COVER_LETTER_INPUT: True,
            locators.COVER_LETTER_SUBMIT: True,
        }
    )
    adapter = ZarplataAdapter()
    adapter._submission_timeout_ms = 0
    adapter._letter_readiness_timeout_ms = 0

    form = await adapter.resume_application(
        page, _plan(), cv_confirmed=True, cover_letter_pending=True
    )
    filled = await adapter.fill_application(page, _plan())
    result = await adapter.submit_application(page)

    assert form.requires_cover_letter
    assert filled.success
    assert result.status == "submitted"
    assert locators.RESPONSE_BUTTON not in page.clicks
    assert page.clicks == [locators.COVER_LETTER_SUBMIT]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("url_after_letter", "expected_exception"),
    [
        ("https://zarplata.ru/vacancy/2", JobDescriptionUnavailable),
        ("https://zarplata.ru/login", AuthenticationPending),
    ],
)
async def test_disappeared_letter_form_on_wrong_or_auth_page_is_not_confirmation(
    url_after_letter, expected_exception
):
    page = _Page(
        visible={
            locators.ALREADY_APPLIED: True,
            locators.COVER_LETTER_INPUT: True,
            locators.COVER_LETTER_SUBMIT: True,
        },
        url_after_letter=url_after_letter,
    )
    adapter = ZarplataAdapter()
    adapter._expected_job_id = "1"
    adapter._letter_readiness_timeout_ms = 0

    assert (await adapter.fill_application(page, _plan())).success
    with pytest.raises(expected_exception):
        await adapter.submit_application(page)

    assert adapter.get_submission_progress()["cover_letter_confirmed"] is False


@pytest.mark.asyncio
async def test_disappeared_letter_form_during_captcha_is_not_confirmation():
    page = _Page(
        visible={
            locators.ALREADY_APPLIED: True,
            locators.COVER_LETTER_INPUT: True,
            locators.COVER_LETTER_SUBMIT: True,
        },
        captcha_after_letter=True,
    )
    adapter = ZarplataAdapter()
    adapter._letter_readiness_timeout_ms = 0

    assert (await adapter.fill_application(page, _plan())).success
    with pytest.raises(CaptchaRequired):
        await adapter.submit_application(page)

    assert adapter.get_submission_progress()["cover_letter_confirmed"] is False


@pytest.mark.asyncio
async def test_reconciliation_ignores_old_letter_flags_and_requires_current_safe_action():
    adapter = ZarplataAdapter()
    adapter._zarplata_cover_letter_confirmed = True
    page = _Page(
        visible={
            locators.ALREADY_APPLIED: True,
            locators.COVER_LETTER_INPUT: True,
            locators.COVER_LETTER_SUBMIT: True,
        }
    )
    page.url = "https://krasnoyarsk.zarplata.ru/vacancy/2"

    progress = await adapter.reconcile_submission_progress(
        page, letter_expected=True
    )

    assert progress == {
        "cv_confirmed": True,
        "cover_letter_pending": True,
        "cover_letter_confirmed": False,
        "letter_recovery_safe": True,
    }
    assert page.clicks == []


@pytest.mark.asyncio
async def test_captcha_after_cv_click_propagates_with_no_false_letter_success():
    page = _Page(
        visible={locators.RESPONSE_BUTTON: True},
        captcha_after_response=True,
    )
    adapter = ZarplataAdapter()

    with pytest.raises(CaptchaRequired):
        await adapter.open_application(page)

    assert adapter.get_submission_progress()["cover_letter_confirmed"] is False
    assert page.clicks == [locators.RESPONSE_BUTTON]


@pytest.mark.asyncio
async def test_unsafe_vacancy_url_is_rejected_before_application_controls():
    page = _Page(visible={locators.RESPONSE_BUTTON: True})
    page.url = "https://example.test/vacancy/1"
    adapter = ZarplataAdapter()

    with pytest.raises(AuthenticationPending):
        await adapter.open_application(page)

    assert page.clicks == []


@pytest.mark.asyncio
async def test_ordinary_letter_is_not_confirmed_without_cv_success():
    page = _Page(
        visible={
            locators.RESPONSE_BUTTON: True,
            locators.COVER_LETTER_INPUT: True,
            locators.RESPONSE_SUBMIT: True,
        },
        ordinary_form=True,
        ordinary_confirm=False,
    )
    adapter = ZarplataAdapter()
    adapter._submission_timeout_ms = 0
    adapter._letter_readiness_timeout_ms = 0

    await adapter.open_application(page)
    assert (await adapter.fill_application(page, _plan())).success
    result = await adapter.submit_application(page)

    assert result.status == "unknown"
    assert adapter.get_submission_progress()["cv_confirmed"] is False
    assert adapter.get_submission_progress()["cover_letter_confirmed"] is False


@pytest.mark.asyncio
async def test_late_letter_field_after_deadline_returns_bounded_pending_failure():
    page = _Page(
        visible={locators.ALREADY_APPLIED: True},
        delayed_letter_ms=10_000,
    )
    adapter = ZarplataAdapter()
    adapter._letter_readiness_timeout_ms = 0

    filled = await adapter.fill_application(page, _plan())

    assert not filled.success
    assert filled.unknown_questions == ["Сопроводительное письмо"]
    assert adapter.get_submission_progress()["cv_confirmed"] is True
    assert adapter.get_submission_progress()["cover_letter_pending"] is True
    assert adapter.get_submission_progress()["cover_letter_diagnostic"]["category"] == "absent"


@pytest.mark.asyncio
async def test_open_application_clears_previous_letter_state_before_captcha():
    page = _Page(
        visible={locators.RESPONSE_BUTTON: True},
        captcha_after_response=True,
    )
    adapter = ZarplataAdapter()
    adapter._zarplata_cv_submission_confirmed = True
    adapter._zarplata_cover_letter_pending = True
    adapter._zarplata_cover_letter_confirmed = True
    adapter._zarplata_cover_letter_diagnostic = {"category": "old"}

    with pytest.raises(CaptchaRequired):
        await adapter.open_application(page)

    progress = adapter.get_submission_progress()
    assert progress["cv_confirmed"] is False
    assert progress["cover_letter_pending"] is False
    assert progress["cover_letter_confirmed"] is False
    assert progress["cover_letter_attempted"] is False
    assert progress["cover_letter_diagnostic"] is None
    assert page.clicks == [locators.RESPONSE_BUTTON]
