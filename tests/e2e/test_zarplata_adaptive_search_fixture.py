"""Synthetic Chromium coverage for Zarplata's paged adaptive source hook."""

from urllib.parse import parse_qs, urlparse

import pytest

from backend.adapters.zarplata.adapter import ZarplataAdapter
from backend.browser.executor import BrowserExecutor
from backend.orchestrator.adaptive_search import AdaptiveSearch


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_chromium_adaptive_zarplata_query_reads_more_than_100_cards(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    executor = BrowserExecutor("zarplata-adaptive-fixture", ZarplataAdapter.allowed_domains, headless=True)
    adapter = ZarplataAdapter()
    engine = AdaptiveSearch(adapter)
    source = engine.add(adapter.query_source("Python"))
    engine.home = source.key
    page_data = {
        0: range(0, 60),
        1: range(60, 120),
        2: range(120, 130),
    }

    try:
        page = await executor.start()

        async def serve_fixture(route):
            requested_page = int(parse_qs(urlparse(route.request.url).query).get("page", [0])[0])
            cards = "".join(
                f'<a data-qa="serp-item__title" href="/vacancy/{external_id}">Fixture {external_id}</a>'
                for external_id in page_data.get(requested_page, ())
            )
            pager = (
                '<div data-qa="pager-block"><a data-qa="pager-next">Next</a></div>'
                if requested_page < 2
                else '<div data-qa="pager-block"></div>'
            )
            await route.fulfill(
                status=200,
                content_type="text/html",
                body=f"<main>{cards}{pager}</main>",
            )

        await page.route("https://zarplata.ru/search/vacancy**", serve_fixture)
        batches = [await engine.collect_more_job_refs(page) for _ in range(3)]

        assert [len(batch) for batch in batches] == [60, 60, 10]
        assert len(engine.seen) == 130
    finally:
        await executor.close()
