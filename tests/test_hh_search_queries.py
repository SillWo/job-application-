from urllib.parse import parse_qs, urlparse

import pytest

from backend.adapters.base.protocol import JobRef
from backend.adapters.hh.adapter import HHAdapter


class _EmptyLocator:
    @property
    def first(self):
        return self

    def filter(self, **_kwargs):
        return self

    async def count(self):
        return 0

    async def is_visible(self):
        return False


class _Page:
    url = "https://hh.ru/"

    def locator(self, *_args, **_kwargs):
        return _EmptyLocator()

    def get_by_text(self, *_args, **_kwargs):
        return _EmptyLocator()

    async def goto(self, url, **_kwargs):
        self.url = url

    async def wait_for_timeout(self, *_args):
        return None


async def _configure(queries):
    adapter = HHAdapter()
    await adapter.open_search(_Page(), {"query": "Desired title", "queries": queries})
    return adapter


@pytest.mark.asyncio
async def test_open_search_uses_only_deduped_queries_and_url_encodes():
    adapter = await _configure(["  Python/Backend ", "python backend", "QA & Automation"])
    assert adapter._search_queries == ["Python Backend", "QA Automation"]
    assert "Desired title" not in adapter._fallback_search_urls[0]
    params = parse_qs(urlparse(adapter._fallback_search_urls[1]).query)
    assert params["text"] == ["QA Automation"]
    assert params["search_field"] == ["name"]


@pytest.mark.asyncio
async def test_empty_query_plan_still_searches_available_vacancies():
    adapter = await _configure([])
    assert adapter._fallback_search_urls == ["https://hh.ru/search/vacancy"]
    assert adapter._search_queries == [""]


@pytest.mark.asyncio
async def test_slow_navigation_has_time_to_finish():
    class SlowPage(_Page):
        async def goto(self, url, **kwargs):
            # A page taking 25 seconds could never succeed with the old timeout.
            if kwargs.get("timeout", 0) < 25_000:
                raise TimeoutError("response headers took 25 seconds")
            self.url = url

    page = SlowPage()
    await HHAdapter().open_job(page, JobRef(external_id="1", url="https://hh.ru/vacancy/1"))
    assert page.url == "https://hh.ru/vacancy/1"


@pytest.mark.asyncio
async def test_empty_page_advances_query_and_resets_cursor_state():
    adapter = await _configure(["one", "two"])
    seen = []

    async def collect(_page, url, number):
        seen.append((parse_qs(urlparse(url).query)["text"][0], number))
        if len(seen) == 1:
            return []
        return [JobRef(external_id="2", url="https://hh.ru/vacancy/2")]

    adapter._collect_search_page = collect
    adapter._last_search_page_signature = ("stale",)
    adapter._repeated_search_pages = 2
    assert await adapter.collect_more_job_refs(_Page()) == []
    assert adapter.search_exhausted is False
    assert [r.external_id for r in await adapter.collect_more_job_refs(_Page())] == ["2"]
    assert seen == [("one", 0), ("two", 0)]
    assert adapter._last_search_page_signature is None
    assert adapter._repeated_search_pages == 0


@pytest.mark.asyncio
async def test_dedupes_refs_across_queries_and_exhausts_after_all():
    adapter = await _configure(["one", "two"])
    calls = []

    async def collect(_page, url, number):
        calls.append((url, number))
        if len(calls) == 1:
            return [JobRef(external_id="1", url="https://hh.ru/vacancy/1")]
        return []

    adapter._collect_search_page = collect
    assert [r.external_id for r in await adapter.collect_more_job_refs(_Page())] == ["1"]
    assert await adapter.collect_more_job_refs(_Page()) == []
    assert not adapter.search_exhausted
    assert await adapter.collect_more_job_refs(_Page()) == []
    assert adapter.search_exhausted is True
    assert len(calls) == 3  # result page, then confirmed empty pages for both queries


@pytest.mark.asyncio
async def test_repeated_page_requests_recovery_instead_of_false_exhaustion():
    adapter = await _configure(["one", "two"])
    seen = []

    async def collect(_page, url, number):
        seen.append(parse_qs(urlparse(url).query)["text"][0])
        if len(seen) == 1:
            adapter._repeated_search_pages = 3
            adapter._last_search_page_signature = ("repeat",)
            return [JobRef(external_id="1", url="https://hh.ru/vacancy/1")]
        assert adapter._repeated_search_pages == 0
        assert adapter._last_search_page_signature is None
        return [JobRef(external_id="2", url="https://hh.ru/vacancy/2")]

    adapter._collect_search_page = collect
    with pytest.raises(RuntimeError, match="повторяет"):
        await adapter.collect_more_job_refs(_Page())
    assert seen == ["one"]
    assert adapter.search_exhausted is False


@pytest.mark.asyncio
async def test_search_continues_beyond_100_pages():
    adapter = await _configure(["one", "two"])
    calls = 0

    async def collect(*_args):
        nonlocal calls
        calls += 1
        return [JobRef(external_id=str(calls), url=f"https://hh.ru/vacancy/{calls}")]

    adapter._collect_search_page = collect
    for _ in range(100):
        assert await adapter.collect_more_job_refs(_Page())
    assert await adapter.collect_more_job_refs(_Page())
    assert calls == 101
    assert adapter.search_exhausted is False


@pytest.mark.asyncio
async def test_navigation_failure_does_not_advance_search_cursor():
    adapter = await _configure(["one"])

    async def fail(*args):
        raise TimeoutError("page timed out")

    adapter._collect_search_page = fail
    with pytest.raises(TimeoutError):
        await adapter.collect_more_job_refs(_Page())
    assert adapter._search_page_number == 0
    assert adapter.search_exhausted is False


@pytest.mark.asyncio
async def test_search_checkpoint_resumes_next_page_with_seen_refs():
    adapter = await _configure(["one"])
    pages = []

    async def collect(page, url, number):
        pages.append(number)
        return [JobRef(external_id=str(number), url=f"https://hh.ru/vacancy/{number}")]

    adapter._collect_search_page = collect
    await adapter.collect_more_job_refs(_Page())
    checkpoint = adapter.search_checkpoint()
    restarted = await _configure(["one"])
    restarted.restore_search_checkpoint(checkpoint)
    restarted._collect_search_page = collect
    assert [ref.external_id for ref in await restarted.collect_more_job_refs(_Page())] == ["1"]
    assert pages == [0, 1]
    assert restarted._search_seen_ids == {"0", "1"}


@pytest.mark.asyncio
@pytest.mark.parametrize("confirmed_empty", [False, True])
async def test_only_explicit_empty_page_confirms_exhaustion(confirmed_empty):
    adapter = await _configure(["one"])

    async def no_links(*args, **kwargs):
        return []

    class EmptyMarker(_EmptyLocator):
        async def count(self):
            return int(confirmed_empty)

        async def is_visible(self):
            return confirmed_empty

    class EmptyPage(_Page):
        def locator(self, *args, **kwargs):
            return EmptyMarker()

    adapter._visible_job_refs = no_links
    if confirmed_empty:
        assert await adapter.collect_more_job_refs(EmptyPage()) == []
        assert adapter.search_exhausted
    else:
        with pytest.raises(RuntimeError, match="не загрузилась"):
            await adapter.collect_more_job_refs(EmptyPage())
        assert not adapter.search_exhausted
