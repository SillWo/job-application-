"""Mock-page coverage for the site-specific public resume import boundary."""

import os

import pytest
from playwright.async_api import async_playwright

from backend.adapters.hh.adapter import HHAdapter
from backend.adapters.hh.resume import validate_resume_url as validate_hh
from backend.adapters.hirehi.adapter import HireHiAdapter
from backend.adapters.hirehi.resume import validate_resume_url as validate_hirehi
from backend.adapters.zarplata.adapter import ZarplataAdapter
from backend.adapters.zarplata.resume import validate_resume_url as validate_zarplata
from backend.schemas.domain import FieldAvailability


@pytest.mark.parametrize(
    ("validator", "url", "host"),
    [
        (validate_hh, "https://hh.ru/resume/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "hh.ru"),
        (validate_hh, "https://region.hh.ru/resume/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "region.hh.ru"),
        (validate_hirehi, "https://hirehi.ru/resume/TestResume_1", "hirehi.ru"),
        (validate_zarplata, "https://krasnoyarsk.zarplata.ru/resume/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", "krasnoyarsk.zarplata.ru"),
    ],
)
def test_public_resume_urls_are_canonicalized(validator, url, host):
    ref = validator(url)
    assert ref.url.startswith(f"https://{host}/resume/")
    assert ref.source_site in {"hh", "hirehi", "zarplata"}


@pytest.mark.parametrize(
    "url",
    [
        "http://hh.ru/resume/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "https://hh.ru.evil.example/resume/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "https://evilhh.ru/resume/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "https://hh.ru/resume/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa?next=https://evil.example",
        "https://hh.ru/resume/../account",
    ],
)
def test_resume_url_policy_rejects_spoof_and_smuggling(url):
    with pytest.raises(ValueError):
        validate_hh(url)


def test_manifest_advertises_optional_resume_capability():
    manifest = HHAdapter.manifest
    assert manifest.supports_resume_import is True
    assert manifest.supports_public_resume_url is True
    assert manifest.supports_account_resume_list is False


@pytest.mark.asyncio
async def test_navigation_rechecks_final_host_and_resume_id():
    class NavPage:
        def __init__(self, final_url):
            self.url = "about:blank"
            self.final_url = final_url

        async def goto(self, _url, **_kwargs):
            self.url = self.final_url

    adapter = HHAdapter()
    ref = adapter.validate_resume_url("https://hh.ru/resume/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
    await adapter.open_resume(
        NavPage("https://siberia.hh.ru/resume/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"), ref
    )
    with pytest.raises(ValueError):
        await adapter.open_resume(
            NavPage("https://hh.ru.evil.example/resume/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"), ref
        )
    with pytest.raises(ValueError):
        await adapter.open_resume(
            NavPage("https://hh.ru/resume/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"), ref
        )


class _MockPage:
    """A minimal locator-only page; no HTML or script execution is involved."""

    def __init__(self, values, url="https://region.hh.ru/resume/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"):
        self.values = values
        self.url = url

    def locator(self, selector):
        for marker, value in self.values.items():
            if marker in selector:
                return _MockLocator(value)
        return _MockLocator([])


class _MockLocator:
    def __init__(self, values):
        self.values = values if isinstance(values, list) else [values]

    async def count(self):
        return len(self.values)

    def nth(self, index):
        return _MockLocator([self.values[index]])

    def locator(self, selector):
        value = self.values[0]
        return value.locator(selector) if hasattr(value, "locator") else _MockLocator([])

    async def is_visible(self):
        value = self.values[0]
        return value is not None

    async def inner_text(self, **_kwargs):
        value = self.values[0]
        if hasattr(value, "inner_text"):
            return await value.inner_text(**_kwargs)
        if isinstance(value, tuple):
            return value[0]
        return value or ""

    async def get_attribute(self, _name):
        value = self.values[0]
        return value[1] if isinstance(value, tuple) and len(value) > 1 else None


class _MockItem:
    def __init__(self, text, children=None):
        self.text = text
        self.children = children or {}

    async def is_visible(self):
        return True

    async def inner_text(self, **_kwargs):
        return self.text

    def locator(self, selector):
        for marker, value in self.children.items():
            if marker in selector:
                return _MockLocator(value)
        return _MockLocator([])


@pytest.mark.asyncio
async def test_hh_extractor_returns_typed_snapshot_and_marks_hidden_contact():
    page = _MockPage({
        "resume-personal-name": "Test Candidate",
        "resume-block-title-position": "Backend Engineer",
        "resume-contacts-phone": [None],
        "resume-contact-email": "candidate@example.invalid",
        "resume-about": "Visible professional summary",
        "resume-skill": ["Python", "SQL"],
    })
    adapter = HHAdapter()
    ref = adapter.validate_resume_url(page.url)
    snapshot = await adapter.extract_resume(page, ref)
    assert snapshot.source_site == "hh"
    assert snapshot.target.desired_title.value == "Backend Engineer"
    assert snapshot.contacts.phone.availability is FieldAvailability.HIDDEN
    assert snapshot.contacts.email.value == "candidate@example.invalid"
    assert snapshot.content_hash and len(snapshot.content_hash) == 64
    assert "identity" in snapshot.coverage.present_sections
    assert "contacts.phone" in snapshot.coverage.hidden_fields


@pytest.mark.asyncio
async def test_hh_title_extractor_ignores_broad_parent_position_container():
    # The broad parent also contains specialization and employment details;
    # only the nested title-position node is the desired job title.
    page = _MockPage({
        "resume-block-position": "Менеджер продукта\nСпециализация: IT\nТип занятости: полная",
        "resume-personal-name": "Test Candidate",
        "resume-block-title-position": "Менеджер продукта",
        "resume-skill": ["SQL"],
    })
    adapter = HHAdapter()
    ref = adapter.validate_resume_url(page.url)
    snapshot = await adapter.extract_resume(page, ref)
    assert snapshot.target.desired_title.value == "Менеджер продукта"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("adapter", "url", "name_selector", "title_selector"),
    [
        (HireHiAdapter, "https://hirehi.ru/resume/TestResume_1", ".resume-public-name", ".resume-public-position"),
        (ZarplataAdapter, "https://region.zarplata.ru/resume/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", "resume-personal-name", "resume-block-title-position"),
    ],
)
async def test_each_site_extractor_returns_the_common_snapshot(adapter, url, name_selector, title_selector):
    page = _MockPage({name_selector: "Candidate", title_selector: "Engineer", "skill": ["Python"]}, url=url)
    instance = adapter()
    snapshot = await instance.extract_resume(page, instance.validate_resume_url(url))
    assert snapshot.source_site in {"hirehi", "zarplata"}
    assert snapshot.identity.full_name.value == "Candidate"
    assert snapshot.target.desired_title.value == "Engineer"


@pytest.mark.asyncio
async def test_hh_fixture_extracts_extended_sections_and_reports_dom_drift():
    valid_experience = _MockItem(
        "Employer Role Summary",
        {
            "resume-experience-company": "Employer",
            "resume-experience-position": "Role",
            "resume-experience-description": "Summary",
        },
    )
    drifted_experience = _MockItem("A card with changed markup")
    page = _MockPage({
        "resume-personal-name": "Candidate",
        "resume-block-title-position": "Engineer",
        "resume-project-item": [_MockItem("Project")],
        "resume-block-education-item": [_MockItem("University")],
        "resume-block-language-item": [_MockItem("English")],
        "resume-course-item": [_MockItem("Course")],
        "resume-certification-item": [_MockItem("Certification")],
        "resume-award-item": [_MockItem("Award")],
        "resume-portfolio-item": [_MockItem("Portfolio")],
        "resume-additional-section": [_MockItem("Additional section")],
        "resume-experience-item": [valid_experience, drifted_experience],
    })
    adapter = HHAdapter()
    ref = adapter.validate_resume_url("https://hh.ru/resume/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
    snapshot = await adapter.extract_resume(page, ref)
    assert len(snapshot.projects) == 1
    assert len(snapshot.education) == 1
    assert len(snapshot.languages) == 1
    assert len(snapshot.courses) == 1
    assert len(snapshot.certifications) == 1
    assert len(snapshot.awards) == 1
    assert len(snapshot.portfolio) == 1
    assert len(snapshot.additional_sections) == 1
    assert snapshot.coverage.parse_errors


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("adapter", "url"),
    [
        (HHAdapter, "https://hh.ru/resume/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"),
        (ZarplataAdapter, "https://region.zarplata.ru/resume/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"),
    ],
)
async def test_owner_experience_cards_fallback_to_visible_duties(adapter, url):
    cards = [_MockItem("Owner employer one — Backend engineer — Built APIs"),
             _MockItem("Owner employer two — Engineer — Improved reliability"),
             _MockItem("Owner employer three — Developer — Led delivery")]
    page = _MockPage({
        "resume-personal-name": "Candidate",
        "resume-block-title-position": "Engineer",
        "profile-experience-company-card": cards,
    }, url=url)
    snapshot = await adapter().extract_resume(page, adapter().validate_resume_url(url))
    assert [item.duties.value for item in snapshot.experience] == [card.text for card in cards]
    assert all(item.duties.availability is FieldAvailability.PRESENT for item in snapshot.experience)
    assert all(item.company.availability is FieldAvailability.UNSUPPORTED for item in snapshot.experience)
    assert all(item.position.availability is FieldAvailability.UNSUPPORTED for item in snapshot.experience)
    assert all(item.start_date.availability is FieldAvailability.UNSUPPORTED for item in snapshot.experience)
    assert not snapshot.coverage.parse_errors


@pytest.mark.asyncio
async def test_removed_public_page_is_not_returned_as_empty_snapshot():
    class UnavailablePage:
        def locator(self, selector):
            return _MockLocator(["Not found"]) if selector == "body" else _MockLocator([])

    adapter = HireHiAdapter()
    ref = adapter.validate_resume_url("https://hirehi.ru/resume/TestResume_1")
    with pytest.raises(ValueError, match="недоступна"):
        await adapter.extract_resume(UnavailablePage(), ref)


@pytest.mark.asyncio
async def test_benign_unavailable_phrase_does_not_block_existing_resume():
    page = _MockPage({
        "body": "Candidate contact unavailable",
        ".resume-public-name": "Candidate",
        ".resume-public-position": "Engineer",
        "skill": ["Python"],
    }, url="https://hirehi.ru/resume/TestResume_1")
    adapter = HireHiAdapter()
    ref = adapter.validate_resume_url(page.url)
    snapshot = await adapter.extract_resume(page, ref)
    assert snapshot.identity.full_name.value == "Candidate"
    assert snapshot.target.desired_title.value == "Engineer"


@pytest.mark.live
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("adapter", "env_name"),
    [
        (HHAdapter, "LIVE_RESUME_URL_HH"),
        (HireHiAdapter, "LIVE_RESUME_URL_HIREHI"),
        (ZarplataAdapter, "LIVE_RESUME_URL_ZARPLATA"),
    ],
)
async def test_live_resume_import_smoke_when_enabled(adapter, env_name):
    """Opt-in live check; URLs remain outside source and are never logged."""
    raw_url = os.environ.get(env_name)
    if not raw_url:
        pytest.skip(f"{env_name} is not set")
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/128 Safari/537.36"
        )
        page = await context.new_page()
        instance = adapter()
        ref = instance.validate_resume_url(raw_url)
        await instance.open_resume(page, ref)
        snapshot = await instance.extract_resume(page, ref)
        assert snapshot.source_site == instance.site_id
        assert snapshot.content_hash
        await context.close()
        await browser.close()
