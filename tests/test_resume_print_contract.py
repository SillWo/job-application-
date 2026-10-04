from __future__ import annotations

import asyncio
from urllib.parse import urlparse, urlunparse

import pytest
from playwright.async_api import async_playwright

from backend.adapters.base.resume_import import (
    ResumeURLPolicy,
    _require_print_layout,
    normalize_gender,
    open_resume_page,
    snapshot_hash,
)
from backend.schemas.domain import (
    FieldAvailability,
    ResumeContacts,
    ResumeExperience,
    ResumeIdentity,
    ResumeLocation,
    SiteResumeSnapshot,
    SourceField,
)

HH = ResumeURLPolicy(
    "hh", "hh.ru", r"/resume/(?P<id>[0-9a-fA-F]{16,64})/?",
    r"[0-9a-fA-F]{16,64}", r"^(?:hh\.ru|www\.hh\.ru|[a-z0-9-]+\.hh\.ru)$",
    redirect_host_group={"hh.ru", "krasnoyarsk.hh.ru"},
)
OFFLINE_HH = ResumeURLPolicy(
    "hh", "resume.test", r"/resume/(?P<id>[0-9a-fA-F]{16,64})/?",
    r"[0-9a-fA-F]{16,64}", r"^resume\.test$",
)


class _HttpPolicy(ResumeURLPolicy):
    """HTTPS policy semantics over a loopback HTTP fixture server."""

    def validate(self, url):
        if str(url).startswith("http://"):
            parsed = urlparse(str(url))
            secure_url = urlunparse(("https", parsed.hostname or "", parsed.path, "", parsed.query, ""))
            secure = super().validate(secure_url)
            canonical = urlunparse(("http", parsed.netloc, parsed.path.rstrip("/"), "", "", ""))
            return secure.model_copy(update={
                "url": canonical,
                "import_url": canonical + "?print=true",
            })
        return super().validate(url)
HIREHI = ResumeURLPolicy(
    "hirehi", "hirehi.ru", r"/resume/(?P<id>[A-Za-z0-9_-]{6,128})/?",
    r"[A-Za-z0-9_-]{6,128}", r"^(?:www\.)?hirehi\.ru$", requires_print=False,
)


@pytest.mark.parametrize(
    "availability",
    [FieldAvailability.NOT_PROVIDED, FieldAvailability.HIDDEN, FieldAvailability.PARSE_ERROR],
)
def test_gender_normalization_preserves_non_present_field_provenance(availability):
    field = SourceField[str](
        value="Мужчина", availability=availability, source_section="identity"
    )
    normalized = normalize_gender(field)
    assert normalized.value == "Мужчина"
    assert normalized.availability is availability
    assert normalized.source_section == "identity"


def test_resume_ref_has_stable_source_and_print_import_url():
    ref = HH.validate("https://hh.ru/resume/aaaaaaaaaaaaaaaa?utm_source=x&from=mail")
    assert ref.url == "https://hh.ru/resume/aaaaaaaaaaaaaaaa"
    assert ref.import_url == "https://hh.ru/resume/aaaaaaaaaaaaaaaa?print=true"
    assert HIREHI.validate("https://hirehi.ru/resume/TestResume_1?from=mail").import_url == (
        "https://hirehi.ru/resume/TestResume_1"
    )
    assert HH.validate("https://hh.ru/resume/aaaaaaaaaaaaaaaa/").url == ref.url
    assert HH.validate("https://hh.ru/resume/aaaaaaaaaaaaaaaa?print=true").url == ref.url


def test_final_navigation_rejects_apex_to_unconfigured_www_host_change():
    with pytest.raises(ValueError, match="домен"):
        HH.validate_final(
            "https://www.hh.ru/resume/aaaaaaaaaaaaaaaa?print=true",
            "aaaaaaaaaaaaaaaa",
            "hh.ru",
        )


def test_configured_hh_apex_to_regional_redirect_keeps_original_source_and_print_import():
    from backend.adapters.hh.resume import POLICY

    original = POLICY.validate("https://hh.ru/resume/aaaaaaaaaaaaaaaa")
    final = POLICY.validate_final(
        "https://krasnoyarsk.hh.ru/resume/aaaaaaaaaaaaaaaa?print=true",
        original.external_id,
        "hh.ru",
    )
    assert original.url == "https://hh.ru/resume/aaaaaaaaaaaaaaaa"
    assert original.import_url == "https://hh.ru/resume/aaaaaaaaaaaaaaaa?print=true"
    assert final.url == "https://krasnoyarsk.hh.ru/resume/aaaaaaaaaaaaaaaa"


def test_hh_policy_rejects_unlisted_subdomain_redirect_even_when_input_matches_regex():
    from backend.adapters.hh.resume import POLICY

    original = POLICY.validate("https://hh.ru/resume/aaaaaaaaaaaaaaaa")
    assert POLICY.validate("https://evil.hh.ru/resume/aaaaaaaaaaaaaaaa").external_id == original.external_id
    with pytest.raises(ValueError):
        POLICY.validate_final(
            "https://evil.hh.ru/resume/aaaaaaaaaaaaaaaa?print=true",
            original.external_id,
            "hh.ru",
        )


def test_non_hh_site_keeps_exact_host_redirect_policy():
    hirehi = ResumeURLPolicy(
        "hirehi", "hirehi.ru", r"/resume/(?P<id>[A-Za-z0-9_-]{6,128})/?",
        r"[A-Za-z0-9_-]{6,128}", r"^(?:hirehi\.ru|[a-z0-9-]+\.hirehi\.ru)$",
        requires_print=False,
    )
    original = hirehi.validate("https://hirehi.ru/resume/TestResume_1")
    with pytest.raises(ValueError):
        hirehi.validate_final(
            "https://evil.hirehi.ru/resume/TestResume_1",
            original.external_id,
            "hirehi.ru",
        )


@pytest.mark.parametrize(
    "url",
    [
        "https://hh.ru/resume/aaaaaaaaaaaaaaaa?print=false",
        "https://hh.ru/resume/aaaaaaaaaaaaaaaa?print=true&print=true",
        "https://hh.ru/resume/aaaaaaaaaaaaaaaa?PRINT=true",
        "https://hh.ru/resume/aaaaaaaaaaaaaaaa?print=true%ZZ",
        "https://hh.ru/resume/aaaaaaaaaaaaaaaa?next=https://evil.example",
        "https://hirehi.ru/resume/TestResume_1?print=true",
    ],
)
def test_print_contract_rejects_conflicts_unknowns_and_malformed_queries(url):
    policy = HIREHI if "hirehi.ru" in url else HH
    with pytest.raises(ValueError):
        policy.validate(url)


class _Page:
    def __init__(self, final_urls: list[str]):
        self.final_urls = list(final_urls)
        self.url = "about:blank"
        self.goto_urls: list[str] = []

    async def goto(self, url, **_kwargs):
        self.goto_urls.append(url)
        self.url = self.final_urls.pop(0)


PRINT_HTML = """<!doctype html><html><body class="bloko-print">
<main data-qa="resume-main-info__content-wrapper"><div data-qa="resume-position">Engineer</div></main>
</body></html>"""
ORDINARY_HTML = """<!doctype html><html><body>
<main data-qa="resume-main-info__content-wrapper"><div data-qa="resume-position">Engineer</div></main>
</body></html>"""


async def _offline_page(browser, route_plan, *, continue_navigation: bool = False,
                        allowed_navigation_hosts: set[str] | None = None):
    context = await browser.new_context(java_script_enabled=False)
    page = await context.new_page()

    async def handler(route):
        request = route.request
        if not request.is_navigation_request():
            await route.abort()
            return
        if continue_navigation:
            netloc = urlparse(request.url).netloc
            if netloc in (allowed_navigation_hosts or set()):
                await route.continue_()
            else:
                await route.abort()
            return
        action = route_plan(request.url)
        if action[0] == "redirect":
            await route.fulfill(status=302, headers={"location": action[1]})
        elif action[0] == "html":
            await route.fulfill(status=200, content_type="text/html", body=action[1])
        else:
            await route.abort()

    await page.route("**/*", handler)
    return context, page


async def _redirect_fixture_server(*, repeat_lost_query: bool = False, body: str = PRINT_HTML):
    state = {"print_hits": 0}

    async def handle(reader, writer):
        request = await reader.readuntil(b"\r\n\r\n")
        target = request.split(b" ", 2)[1].decode("ascii")
        if target == "/resume/aaaaaaaaaaaaaaaa?print=true":
            state["print_hits"] += 1
            if repeat_lost_query or state["print_hits"] == 1:
                response = (
                    b"HTTP/1.1 302 Found\r\nLocation: /resume/aaaaaaaaaaaaaaaa\r\n"
                    b"Content-Length: 0\r\nConnection: close\r\n\r\n"
                )
            else:
                payload = body.encode()
                response = (
                    f"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: {len(payload)}\r\n"
                    "Connection: close\r\n\r\n"
                ).encode() + payload
        else:
            payload = body.encode()
            response = (
                f"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: {len(payload)}\r\n"
                "Connection: close\r\n\r\n"
            ).encode() + payload
        writer.write(response)
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    return server, state


@pytest.mark.asyncio
async def test_offline_browser_print_navigation_retries_only_lost_query():
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        server, state = await _redirect_fixture_server()
        port = server.sockets[0].getsockname()[1]
        policy = _HttpPolicy(
            "hh", "127.0.0.1", r"/resume/(?P<id>[0-9a-fA-F]{16,64})/?",
            r"[0-9a-fA-F]{16,64}", r"^127\.0\.0\.1$",
        )
        context, page = await _offline_page(
            browser, lambda _url: ("abort", ""), continue_navigation=True,
            allowed_navigation_hosts={f"127.0.0.1:{port}"},
        )
        try:
            ref = policy.validate(f"http://127.0.0.1:{port}/resume/aaaaaaaaaaaaaaaa")
            await open_resume_page(page, ref, policy)
            assert page.url == ref.import_url
            await _require_print_layout(page, ref)
            assert await page.locator("body.bloko-print").count() == 1
            assert state["print_hits"] == 2
        finally:
            await context.close()
            await browser.close()
            server.close()
            await server.wait_closed()


@pytest.mark.asyncio
async def test_offline_browser_repeated_lost_query_fails_after_one_retry():
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        server, _state = await _redirect_fixture_server(repeat_lost_query=True)
        port = server.sockets[0].getsockname()[1]
        policy = _HttpPolicy(
            "hh", "127.0.0.1", r"/resume/(?P<id>[0-9a-fA-F]{16,64})/?",
            r"[0-9a-fA-F]{16,64}", r"^127\.0\.0\.1$",
        )
        context, page = await _offline_page(
            browser, lambda _url: ("abort", ""), continue_navigation=True,
            allowed_navigation_hosts={f"127.0.0.1:{port}"},
        )
        try:
            ref = policy.validate(f"http://127.0.0.1:{port}/resume/aaaaaaaaaaaaaaaa")
            with pytest.raises(ValueError):
                await open_resume_page(page, ref, policy)
        finally:
            await context.close()
            await browser.close()
            server.close()
            await server.wait_closed()


@pytest.mark.asyncio
async def test_offline_browser_print_url_with_regular_dom_fails():
    def plan(url):
        if url.endswith("?print=true"):
            return ("html", ORDINARY_HTML)
        return ("abort", "")

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        context, page = await _offline_page(browser, plan)
        try:
            ref = OFFLINE_HH.validate("https://resume.test/resume/aaaaaaaaaaaaaaaa")
            with pytest.raises(ValueError):
                await open_resume_page(page, ref, OFFLINE_HH)
        finally:
            await context.close()
            await browser.close()


@pytest.mark.parametrize("destination", [
    "https://foreign.test/resume/aaaaaaaaaaaaaaaa?print=true",
    "https://resume.test/resume/bbbbbbbbbbbbbbbb?print=true",
])
@pytest.mark.asyncio
async def test_offline_browser_foreign_host_or_id_fails_before_retry(destination):
    ref = OFFLINE_HH.validate("https://resume.test/resume/aaaaaaaaaaaaaaaa")
    page = _Page([destination])
    with pytest.raises(ValueError):
        await open_resume_page(page, ref, OFFLINE_HH)
    assert page.goto_urls == [ref.import_url]


@pytest.mark.asyncio
async def test_lost_print_query_retries_same_validated_url_once():
    ref = HH.validate("https://hh.ru/resume/aaaaaaaaaaaaaaaa")
    page = _Page([
        "https://hh.ru/resume/aaaaaaaaaaaaaaaa",
        "https://hh.ru/resume/aaaaaaaaaaaaaaaa?print=true",
    ])
    await open_resume_page(page, ref, HH)
    assert page.goto_urls == [ref.import_url, ref.import_url]


@pytest.mark.asyncio
async def test_spoofed_redirect_is_rejected_before_retry():
    ref = HH.validate("https://hh.ru/resume/aaaaaaaaaaaaaaaa")
    page = _Page(["https://evil.hh.ru/resume/aaaaaaaaaaaaaaaa"])
    with pytest.raises(ValueError):
        await open_resume_page(page, ref, HH)
    assert page.goto_urls == [ref.import_url]


@pytest.mark.asyncio
async def test_open_resume_allows_hh_apex_to_configured_regional_redirect():
    from backend.adapters.hh.resume import POLICY

    ref = POLICY.validate("https://hh.ru/resume/aaaaaaaaaaaaaaaa")
    page = _Page(["https://krasnoyarsk.hh.ru/resume/aaaaaaaaaaaaaaaa?print=true"])
    await open_resume_page(page, ref, POLICY)
    assert page.goto_urls == ["https://hh.ru/resume/aaaaaaaaaaaaaaaa?print=true"]


@pytest.mark.asyncio
async def test_regional_redirect_is_rejected_before_retry():
    policy = ResumeURLPolicy(
        "hh", "hh.ru", r"/resume/(?P<id>[0-9a-fA-F]{16,64})/?",
        r"[0-9a-fA-F]{16,64}", r"^(?:www\.)?hh\.ru$|^[a-z0-9-]+\.hh\.ru$",
    )
    ref = policy.validate("https://region.hh.ru/resume/aaaaaaaaaaaaaaaa")
    page = _Page(["https://other.hh.ru/resume/aaaaaaaaaaaaaaaa"])
    with pytest.raises(ValueError):
        await open_resume_page(page, ref, policy)
    assert page.goto_urls == [ref.import_url]


def _snapshot() -> SiteResumeSnapshot:
    return SiteResumeSnapshot(
        extractor_version="test-v1",
        source_site="hh",
        source_resume_id="aaaaaaaaaaaaaaaa",
        source_url_hash="a" * 64,
        content_hash="0" * 64,
        identity=ResumeIdentity(
            full_name=SourceField(value="Candidate", availability=FieldAvailability.PRESENT),
            photo_url=SourceField(value="https://cdn.invalid/photo", availability=FieldAvailability.PRESENT),
        ),
        contacts=ResumeContacts(
            preferred_contact=SourceField(value="email", availability=FieldAvailability.PRESENT),
        ),
        location=ResumeLocation(
            commute_time=SourceField(value="30 min", availability=FieldAvailability.PRESENT),
        ),
        experience=[ResumeExperience(
            company="Acme",
            location="Krasnoyarsk",
            company_url="https://acme.invalid",
            industries=["software"],
            employment_type="full_time",
            work_format="remote",
            grade="senior",
            duration="2 years",
        )],
    )


def test_semantic_hash_ignores_import_and_layout_metadata():
    first = _snapshot()
    second = first.model_copy(deep=True, update={
        "schema_version": 999,
        "extractor_version": "other-v9",
        "source_url_hash": "b" * 64,
        "imported_at": first.imported_at.replace(year=2030),
        "coverage": first.coverage.model_copy(update={"hidden_fields": ["layout.changed"]}),
    })
    assert snapshot_hash(first) == snapshot_hash(second)
