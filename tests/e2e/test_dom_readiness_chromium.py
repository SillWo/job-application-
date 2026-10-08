"""Real Chromium regression for delayed vacancy detail markup."""

import pytest

from backend.adapters.base.errors import JobDescriptionUnavailable
from backend.adapters.hh.adapter import HHAdapter
from backend.browser.executor import BrowserExecutor


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_chromium_reads_description_rendered_after_dom_commit(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    executor = BrowserExecutor("dom-readiness-regression", ("127.0.0.1",), headless=True)
    adapter = HHAdapter()
    url = "https://hh.ru/vacancy/fixture"
    html = """
    <h1 data-qa="vacancy-title">Fixture engineer</h1>
    <div data-qa="vacancy-company-name">Fixture company</div>
    <script>
      window.setTimeout(() => {
        const description = document.createElement('div');
        description.dataset.qa = 'vacancy-description';
        description.textContent = 'Description rendered after commit';
        document.body.append(description);
      }, 100);
    </script>
    """

    try:
        page = await executor.start()

        async def serve_fixture(route):
            await route.fulfill(status=200, content_type="text/html", body=html)

        await page.route(url, serve_fixture)
        await page.goto(url, wait_until="commit")
        posting = await adapter.extract_job(page)

        assert posting.external_id == "fixture"
        assert posting.description == "Description rendered after commit"
    finally:
        await executor.close()


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_chromium_standalone_set_content_page_remains_supported(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    executor = BrowserExecutor("dom-readiness-standalone", ("127.0.0.1",), headless=True)
    adapter = HHAdapter()
    try:
        page = await executor.start()
        await page.set_content(
            '<h1 data-qa="vacancy-title">Fixture engineer</h1>'
            '<div data-qa="vacancy-company-name">Fixture company</div>'
            '<div data-qa="vacancy-description"></div>'
        )
        with pytest.raises(JobDescriptionUnavailable) as caught:
            await adapter.extract_job(page)
        assert caught.value.title == "Fixture engineer"
        assert caught.value.company == "Fixture company"
    finally:
        await executor.close()
