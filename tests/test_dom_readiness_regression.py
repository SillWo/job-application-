"""Readiness and safe failure handling for job detail DOMs."""

from __future__ import annotations

import pytest

from backend.adapters.base.errors import CaptchaRequired, JobDescriptionUnavailable
from backend.adapters.base.protocol import JobRef
from backend.adapters.hh import locators as hh_locators
from backend.adapters.hh.adapter import HHAdapter
from backend.adapters.zarplata import locators as zarplata_locators
from backend.adapters.zarplata.adapter import ZarplataAdapter
from backend.orchestrator.recovery import AuthenticationPending


class _Locator:
    def __init__(self, page, selector, index=0):
        self.page = page
        self.selector = selector
        self.index = index
        self.first = self

    def nth(self, index):
        return _Locator(self.page, self.selector, index)

    async def count(self):
        return len(self.page.values.get(self.selector, []))

    async def is_visible(self):
        values = self.page.values.get(self.selector, [])
        if self.index >= len(values):
            return False
        return values[self.index][1]

    async def inner_text(self, **_kwargs):
        values = self.page.values.get(self.selector, [])
        if self.selector == "body":
            return self.page.body
        if self.index >= len(values):
            return ""
        value, _visible, error = values[self.index]
        if error:
            raise RuntimeError(error)
        return value

    async def get_attribute(self, _name):
        return None


class _Page:
    def __init__(self, url, values=None, *, delayed=None, body=""):
        self.url = url
        self.values = values or {}
        self.delayed = delayed
        self.elapsed_ms = 0
        self.body = body

    def locator(self, selector):
        return _Locator(self, selector)

    async def wait_for_timeout(self, milliseconds):
        self.elapsed_ms += milliseconds
        if self.delayed and self.elapsed_ms >= self.delayed[0]:
            self.values.update(self.delayed[1])
            self.delayed = None

    async def goto(self, url, **_kwargs):
        self.url = url


class _ImmediatePage(_Page):
    """Synchronous DOM fake: exercise compatibility without a polling API."""

    wait_for_timeout = None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "adapter_type,locators,url",
    [
        (HHAdapter, hh_locators, "https://hh.ru/vacancy/123"),
        (ZarplataAdapter, zarplata_locators, "https://krasnoyarsk.zarplata.ru/vacancy/123"),
    ],
)
async def test_description_arriving_after_dom_commit_is_read(adapter_type, locators, url):
    adapter = adapter_type()
    adapter._vacancy_readiness_timeout_ms = 500
    page = _Page(
        url,
        {
            locators.VACANCY_TITLE: [("Backend engineer", True, None)],
            locators.COMPANY: [("Example company", True, None)],
        },
        delayed=(100, {locators.DESCRIPTION: [("Delayed description", True, None)]}),
    )

    posting = await adapter.extract_job(page)

    assert posting.description == "Delayed description"
    assert page.elapsed_ms >= 100


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "adapter_type,locators,url",
    [
        (HHAdapter, hh_locators, "https://hh.ru/vacancy/123?token=private"),
        (ZarplataAdapter, zarplata_locators, "https://krasnoyarsk.zarplata.ru/vacancy/123?token=private"),
    ],
)
async def test_blank_description_failure_has_bounded_safe_diagnostics(adapter_type, locators, url):
    adapter = adapter_type()
    page = _ImmediatePage(
        url,
        {
            locators.VACANCY_TITLE: [("Backend engineer", True, None)],
            locators.COMPANY: [("Example company", True, None)],
            locators.DESCRIPTION: [("  ", True, None)],
        },
    )

    with pytest.raises(JobDescriptionUnavailable) as caught:
        await adapter.extract_job(page)

    diagnostics = caught.value.diagnostics
    assert caught.value.title == "Backend engineer"
    assert diagnostics["url_path"] == "/vacancy/123"
    assert diagnostics["external_id"] == "123"
    assert diagnostics["fields"]["description"]["states"] == ["blank"]
    assert "private" not in repr(diagnostics)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state,visible,error",
    [
        ("hidden", False, None),
        ("detached", True, "element detached"),
        ("read-error", True, "cannot read node"),
        ("timeout", True, "operation timeout"),
    ],
)
async def test_description_diagnostics_distinguish_hidden_and_read_failures(state, visible, error):
    adapter = HHAdapter()
    page = _ImmediatePage(
        "https://hh.ru/vacancy/123",
        {
            hh_locators.VACANCY_TITLE: [("Backend engineer", True, None)],
            hh_locators.COMPANY: [("Example company", True, None)],
            hh_locators.DESCRIPTION: [("", visible, error)],
        },
    )

    with pytest.raises(JobDescriptionUnavailable) as caught:
        await adapter.extract_job(page)

    assert caught.value.diagnostics["fields"]["description"]["states"] == [state]


@pytest.mark.asyncio
async def test_absent_description_diagnostic_is_explicit():
    adapter = HHAdapter()
    page = _ImmediatePage(
        "https://hh.ru/vacancy/123",
        {
            hh_locators.VACANCY_TITLE: [("Backend engineer", True, None)],
            hh_locators.COMPANY: [("Example company", True, None)],
        },
    )

    with pytest.raises(JobDescriptionUnavailable) as caught:
        await adapter.extract_job(page)

    assert caught.value.diagnostics["fields"]["description"] == {
        "count": 0,
        "visibility": [],
        "states": ["absent"],
        "outcome": "timeout",
    }


@pytest.mark.asyncio
async def test_expected_vacancy_identity_rejects_redirected_detail_page():
    adapter = HHAdapter()
    adapter._expected_job_id = "expected"
    page = _Page(
        "https://hh.ru/vacancy/other?secret=value",
        {
            hh_locators.VACANCY_TITLE: [("Other title", True, None)],
            hh_locators.COMPANY: [("Other company", True, None)],
            hh_locators.DESCRIPTION: [("Other description", True, None)],
        },
    )

    with pytest.raises(JobDescriptionUnavailable) as caught:
        await adapter.extract_job(page)

    assert caught.value.diagnostics["external_id"] == "other"
    assert "secret" not in repr(caught.value.diagnostics)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "adapter_type,url",
    [
        (HHAdapter, "https://hh.ru/account/login?from=private"),
        (ZarplataAdapter, "https://zarplata.ru/account/login?from=private"),
    ],
)
async def test_login_redirect_uses_authentication_pending(adapter_type, url):
    with pytest.raises(AuthenticationPending):
        await adapter_type().extract_job(_Page(url))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "adapter_type,url,body",
    [
        (HHAdapter, "https://hh.ru/vacancy/123", "Подтвердите, что вы не робот"),
        (ZarplataAdapter, "https://zarplata.ru/vacancy/123", "captcha"),
    ],
)
async def test_captcha_during_readiness_is_not_a_missing_description(adapter_type, url, body):
    adapter = adapter_type()
    page = _Page(
        url,
        body=body,
    )

    with pytest.raises(CaptchaRequired):
        await adapter.extract_job(page)


@pytest.mark.asyncio
async def test_open_job_identity_uses_external_id_from_ref():
    adapter = HHAdapter()
    adapter._vacancy_readiness_timeout_ms = 10
    page = _Page("https://hh.ru/vacancy/other")
    ref = JobRef(external_id="expected", url="https://hh.ru/vacancy/expected")
    await adapter.open_job(page, ref)
    page.url = "https://hh.ru/vacancy/other"

    with pytest.raises(JobDescriptionUnavailable):
        await adapter.extract_job(page)

    assert adapter._expected_job_id == ref.external_id


@pytest.mark.asyncio
async def test_hh_cover_letter_progress_exposes_only_bounded_field_state():
    class DisabledField:
        async def count(self):
            return 1

        async def is_visible(self):
            return True

        async def is_enabled(self):
            return False

    adapter = HHAdapter()
    adapter._hh_cover_letter_field_diagnostic = await adapter._cover_letter_field_diagnostic(
        DisabledField(), "disabled"
    )

    diagnostic = adapter.get_submission_progress()["cover_letter_diagnostic"]

    assert diagnostic == {
        "category": "disabled",
        "count": 1,
        "visible": True,
        "enabled": False,
        "exception_category": None,
    }
