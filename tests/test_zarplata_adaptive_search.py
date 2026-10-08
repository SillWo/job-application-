"""Zarplata adapter contract with the shared durable AdaptiveSearch engine."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest

from backend.adapters.zarplata import locators
from backend.adapters.zarplata.adapter import ZarplataAdapter
from backend.orchestrator import adaptive_search as adaptive_search_module
from backend.orchestrator.adaptive_search import AdaptiveSearch
from backend.orchestrator.recovery import AuthenticationPending, CaptchaRequired
from backend.orchestrator.search_scheduler import Source


class _Link:
    def __init__(self, href="", *, text="", visible=True):
        self.href, self.text, self.visible = href, text, visible

    async def wait_for(self, **_kwargs):
        return None

    async def count(self):
        return 1

    async def is_visible(self):
        return self.visible

    async def get_attribute(self, name):
        return self.href if name == "href" else None

    async def inner_text(self, **_kwargs):
        return self.text


class _LinkList:
    def __init__(self, links=()):
        self.links = list(links)
        self.first = self.links[0] if self.links else self

    async def wait_for(self, **_kwargs):
        return None

    async def count(self):
        return len(self.links)

    def nth(self, index):
        return self.links[index]

    async def is_visible(self):
        return False

    def filter(self, **_kwargs):
        return self


class _Page:
    def __init__(self, pages=None, *, sources=(), related=(), redirect=None, body=""):
        self.url = "https://zarplata.ru/"
        self.pages = pages or {}
        self.sources = list(sources)
        self.related = list(related)
        self.redirect = redirect
        self.body = body
        self.visited = []

    async def goto(self, url, **_kwargs):
        self.visited.append(url)
        self.url = self.redirect or url

    async def wait_for_timeout(self, _milliseconds):
        return None

    def get_by_text(self, *_args, **_kwargs):
        return _LinkList()

    def _page_number(self):
        return int(parse_qs(urlparse(self.url).query).get("page", [0])[0])

    def locator(self, selector):
        if selector == locators.VACANCY_LINK:
            ids = self.pages.get(self._page_number(), [])
            return _LinkList([_Link(f"/vacancy/{external_id}") for external_id in ids])
        if selector == locators.DISCOVERY_LINKS:
            return _LinkList(self.sources)
        if selector == locators.RELATED_VACANCIES:
            return _LinkList(self.related)
        if selector == locators.SEARCH_PAGER:
            return _LinkList([_Link()])
        if selector == locators.SEARCH_NEXT:
            return _LinkList([_Link(visible=self._page_number() < 2)])
        if selector == locators.SEARCH_EMPTY:
            return _LinkList([_Link(visible=self._page_number() in self.pages and not self.pages[self._page_number()])])
        if selector == "body":
            return _Body(self.body)
        return _LinkList()


class _Body:
    def __init__(self, text):
        self.text = text
        self.first = self

    async def count(self):
        return 1

    async def inner_text(self, **_kwargs):
        return self.text


@pytest.mark.asyncio
async def test_open_search_seeds_recommendations_curated_queries_and_broad_coverage():
    adapter = ZarplataAdapter()
    page = _Page()
    engine = AdaptiveSearch(adapter)

    await engine.open_search(
        page,
        {"portfolio_queries": [{"query": "Python backend", "field": "name", "cluster": "backend"}]},
    )

    sources = list(engine.scheduler.sources.values())
    assert any(source.kind == "recommendations" and source.spec["url"] == adapter.home_url for source in sources)
    assert any(source.kind == "query" and parse_qs(urlparse(source.spec["url"]).query)["text"] == ["Python backend"] for source in sources)
    assert any(source.kind == "coverage" and not parse_qs(urlparse(source.spec["url"]).query).get("text") for source in sources)


@pytest.mark.asyncio
async def test_adaptive_query_reads_more_than_100_refs_across_pages_and_dedupes():
    adapter = ZarplataAdapter()
    engine = AdaptiveSearch(adapter)
    source = engine.add(adapter.query_source("Python"))
    engine.home = source.key
    page = _Page(
        {
            0: [str(index) for index in range(60)],
            1: [str(index) for index in range(59, 119)],
            2: [str(index) for index in range(119, 130)],
        }
    )

    batches = [await engine.collect_more_job_refs(page) for _ in range(3)]

    assert [len(batch) for batch in batches] == [60, 59, 11]
    assert len(engine.seen) == 130
    assert source.exhausted
    assert [parse_qs(urlparse(url).query)["page"][0] for url in page.visited] == ["0", "1", "2"]


@pytest.mark.asyncio
async def test_adaptive_checkpoint_resumes_zarplata_cursor_and_seen_ids():
    adapter = ZarplataAdapter()
    engine = AdaptiveSearch(adapter)
    source = engine.add(adapter.query_source("Python"))
    engine.home = source.key
    page = _Page({0: [str(index) for index in range(60)], 1: [str(index) for index in range(60, 120)]})
    first = await engine.collect_more_job_refs(page)
    checkpoint = engine.search_checkpoint()

    resumed = AdaptiveSearch(adapter)
    resumed.restore_search_checkpoint(checkpoint)
    second = await resumed.collect_more_job_refs(page)

    assert len(first) == 60
    assert [ref.external_id for ref in second] == [str(index) for index in range(60, 120)]
    assert len(resumed.seen) == 120
    assert resumed.scheduler.sources[source.key].page == 2


@pytest.mark.asyncio
async def test_expansion_runs_after_50_observations_with_zero_relevant_examples(monkeypatch):
    adapter = ZarplataAdapter()
    engine = AdaptiveSearch(adapter, gateway=object())
    observed = []

    async def plan(_gateway, _resumes, _policy, **kwargs):
        observed.append(kwargs)
        return []

    monkeypatch.setattr(adaptive_search_module, "plan_portfolio", plan)
    for index in range(50):
        await engine.observe(
            _Page(),
            type("Posting", (), {"external_id": str(index), "title": "Engineer", "description": "Role"})(),
            "skip",
            0.1,
        )

    await engine.expand()

    assert len(observed) == 1
    assert observed[0]["relevant"] == []
    assert engine.next_expansion == 100


@pytest.mark.asyncio
async def test_relevant_detail_adds_only_observed_employer_and_related_links():
    adapter = ZarplataAdapter()
    page = _Page(
        sources=[
            _Link("/employer/42", text="Example employer"),
            _Link("https://evil.example/employer/9", text="External employer"),
            _Link("/search/vacancy?text=python", text="Python vacancies"),
        ],
        related=[_Link("/vacancy/12"), _Link("https://evil.example/vacancy/13"), _Link("/vacancy/12")],
    )
    page.url = "https://zarplata.ru/vacancy/1"

    listing_sources = await adapter.collect_visible_sources(page, context="listing")
    relevant_sources = await adapter.collect_visible_sources(page, context="relevant")
    related = await adapter.collect_related_refs(page)

    assert not any(source["kind"] == "employer" for source in listing_sources)
    assert [source["url"] for source in relevant_sources if source["kind"] == "employer"] == [
        "https://zarplata.ru/employer/42"
    ]
    assert [ref.external_id for ref in related] == ["12"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "redirect,error_type",
    [
        ("https://evil.example/search/vacancy?text=Python&page=1", ValueError),
        ("https://zarplata.ru/account/login", AuthenticationPending),
    ],
)
async def test_discovery_rejects_wrong_domain_and_login_redirects(redirect, error_type):
    adapter = ZarplataAdapter()
    source = adapter.query_source("Python")
    page = _Page(redirect=redirect)

    with pytest.raises(error_type):
        await adapter.read_discovery_page(page, source, 1)


@pytest.mark.asyncio
async def test_discovery_raises_for_captcha_stale_pages_and_unverified_empty_results():
    adapter = ZarplataAdapter()
    source = adapter.query_source("Python")

    with pytest.raises(CaptchaRequired):
        await adapter.read_discovery_page(_Page(body="captcha"), source, 0)

    stale_page = _Page(redirect="https://zarplata.ru/search/vacancy?text=Python&page=0")
    with pytest.raises(RuntimeError, match="номер страницы"):
        await adapter.read_discovery_page(stale_page, source, 1)

    unavailable_page = _Page({0: []})
    unavailable_page.locator = lambda selector: (
        _LinkList() if selector in {locators.SEARCH_PAGER, locators.SEARCH_EMPTY} else _Page.locator(unavailable_page, selector)
    )
    with pytest.raises(RuntimeError, match="отсутствие вакансий не подтверждено"):
        await adapter.read_discovery_page(unavailable_page, source, 0)


def test_discovery_sources_reject_unknown_domains_and_unsupported_url_parameters():
    adapter = ZarplataAdapter()

    with pytest.raises(ValueError):
        adapter.validate_search_source({"url": "https://evil.example/search/vacancy?text=x", "kind": "query"})
    with pytest.raises(ValueError):
        adapter.validate_search_source({"url": "https://zarplata.ru/search/vacancy?token=secret", "kind": "query"})


def test_adaptive_search_canonicalizes_only_known_tracking_parameters():
    adapter = ZarplataAdapter()
    engine = AdaptiveSearch(adapter)
    tracked_url = (
        "https://krasnoyarsk.zarplata.ru/search/vacancy?text=product&area=1"
        "&hhtmFrom=main&hhtmFromLabel=vacancy_search_line&suggestId=abc"
    )

    source = engine.add({"url": tracked_url, "kind": "query"})
    repeated = engine.add({"url": "https://krasnoyarsk.zarplata.ru/search/vacancy?area=1&text=product", "kind": "query"})

    assert source.spec["url"] == "https://krasnoyarsk.zarplata.ru/search/vacancy?area=1&text=product"
    assert repeated.key == source.key
    assert len(engine.scheduler.sources) == 1


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/search/vacancy?text=product&hhtmFrom=main",
        "https://zarplata.ru/search/vacancy?text=product&redirect=https%3A%2F%2Fevil.example",
        "https://zarplata.ru/search/vacancy?text=product&text=other",
        "https://zarplata.ru/search/vacancy?resume=https%3A%2F%2Fevil.example",
        "https://zarplata.ru/search/vacancy?resume=0123456789abcdef0123456789abcdef&resume=0123456789abcdef0123456789abcdef",
    ],
)
def test_adaptive_search_still_rejects_forged_or_ambiguous_urls(url):
    engine = AdaptiveSearch(ZarplataAdapter())

    with pytest.raises(ValueError):
        engine.add({"url": url, "kind": "query"})


@pytest.mark.asyncio
async def test_visible_source_with_known_trackers_is_canonicalized_not_dropped():
    adapter = ZarplataAdapter()
    page = _Page(
        sources=[
            _Link(
                "https://zarplata.ru/search/vacancy?text=python&hhtmFrom=main"
                "&hhtmFromLabel=vacancy_search_line&suggestId=abc",
                text="Python vacancies",
            )
        ]
    )
    page.url = "https://zarplata.ru/vacancy/1"

    sources = await adapter.collect_visible_sources(page)

    assert [source["url"] for source in sources] == [
        "https://zarplata.ru/search/vacancy?text=python"
    ]


@pytest.mark.asyncio
async def test_recommendation_resume_id_survives_add_and_visible_source_canonicalization():
    adapter = ZarplataAdapter()
    engine = AdaptiveSearch(adapter)
    resume_id = "0123456789abcdef0123456789abcdef"
    tracked_url = (
        "https://krasnoyarsk.zarplata.ru/search/vacancy?resume="
        f"{resume_id}&hhtmFrom=main&hhtmFromLabel=vacancy_search_line"
    )

    source = engine.add({"url": tracked_url, "kind": "recommendations"})
    page = _Page(
        sources=[
            _Link(tracked_url, text="Подходящие вакансии")
        ]
    )
    page.url = "https://krasnoyarsk.zarplata.ru/"
    visible_sources = await adapter.collect_visible_sources(page)

    canonical_url = f"https://krasnoyarsk.zarplata.ru/search/vacancy?resume={resume_id}"
    assert source.spec["url"] == canonical_url
    assert [item["url"] for item in visible_sources] == [canonical_url]


@pytest.mark.asyncio
async def test_discovery_preserves_resume_id_and_rejects_redirect_to_another_resume():
    adapter = ZarplataAdapter()
    resume_id = "0123456789abcdef0123456789abcdef"
    other_resume_id = "fedcba9876543210fedcba9876543210"
    source = {
        "url": f"https://krasnoyarsk.zarplata.ru/search/vacancy?resume={resume_id}",
        "kind": "recommendations",
    }
    expected_page_url = f"{source['url']}&page=0"
    page = _Page({0: ["1"]})

    await adapter.read_discovery_page(page, source, 0)

    assert page.visited == [expected_page_url]
    redirected = _Page(
        {0: ["1"]},
        redirect=(
            "https://krasnoyarsk.zarplata.ru/search/vacancy?resume="
            f"{other_resume_id}&page=0"
        ),
    )
    with pytest.raises(RuntimeError, match="параметры источника"):
        await adapter.read_discovery_page(redirected, source, 0)


def test_scheduler_prefers_measured_relevance_without_discarding_other_sources():
    adapter = ZarplataAdapter()
    engine = AdaptiveSearch(adapter)
    weak = Source("weak", "query", adapter.query_source("weak"), raw=20, novel=20, judged=20, relevant=0, seconds=10, visits=1)
    strong = Source("strong", "query", adapter.query_source("strong"), raw=20, novel=20, judged=20, relevant=20, seconds=10, visits=1)
    engine.scheduler.add(weak)
    engine.scheduler.add(strong)

    chosen = engine.scheduler.choose()

    assert chosen.key == "strong"
    assert engine.scheduler.sources["weak"].exhausted is False


@pytest.mark.asyncio
async def test_failed_source_and_exhausted_epoch_keep_bounded_backoff_in_checkpoint():
    adapter = ZarplataAdapter()
    engine = AdaptiveSearch(adapter)
    source = engine.add(adapter.query_source("Python"))
    engine.home = source.key
    page = _Page(redirect="https://evil.example/search/vacancy?text=Python&page=0")

    assert await engine.collect_more_job_refs(page) == []
    assert source.failures == 1
    assert 0 < engine.next_retry_delay() <= 5
    failed_checkpoint = engine.search_checkpoint()
    restored = AdaptiveSearch(adapter)
    restored.restore_search_checkpoint(failed_checkpoint)
    assert restored.scheduler.sources[source.key].failures == 1

    exhausted = AdaptiveSearch(adapter)
    final_source = exhausted.add(adapter.query_source("empty"))
    exhausted.home = final_source.key
    final_source.exhausted = True
    assert await exhausted.collect_more_job_refs(page) == []
    assert exhausted.search_exhausted
    assert 0 < exhausted.next_retry_delay() <= 5
