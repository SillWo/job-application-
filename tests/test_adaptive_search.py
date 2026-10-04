import json
from collections import Counter
from types import SimpleNamespace

import pytest

from backend.adapters.base.protocol import JobRef
from backend.adapters.hh.adapter import HHAdapter
from backend.intelligence.adaptive_search_planner import PortfolioPlan, plan_portfolio
from backend.orchestrator.adaptive_search import AdaptiveSearch
from backend.orchestrator.search_scheduler import Scheduler, Source


def refs(*ids):
    return [JobRef(external_id=str(i), url=f"https://hh.ru/vacancy/{i}") for i in ids]


class Adapter(HHAdapter):
    def __init__(self):
        self.calls = []
        self.batches = {}

    async def open_search(self, page, filters):
        page.url = self.home_url

    async def collect_job_refs(self, page):
        return refs(1)

    async def collect_visible_sources(self, page, context="listing"):
        return []

    async def collect_related_refs(self, page):
        return refs(2, 2, 1)

    async def read_discovery_page(self, page, spec, number):
        self.calls.append((spec["url"], number))
        response = self.batches.get((spec["url"], number), {"refs": [], "terminal": True})
        if isinstance(response, Exception):
            raise response
        return response


async def controller(queries=None):
    adapter, page = Adapter(), SimpleNamespace(url="https://hh.ru/")
    search = AdaptiveSearch(adapter)
    await search.open_search(page, {"portfolio_queries": queries or []})
    await search.collect_job_refs(page)
    return search, adapter, page


def test_exploration_covers_every_source_despite_low_yield_and_rec_priority():
    scheduler = Scheduler()
    for i in range(30):
        scheduler.add(Source(str(i), "coverage", {}, relevant=0, judged=100))
    scheduler.add(Source("rec", "recommendations", {}, relevant=100, judged=100))
    visits = Counter(scheduler.choose().key for _ in range(200))
    assert set(visits) == set(scheduler.sources)
    assert visits["rec"] == 120


def test_discounted_yield_changes_priority_without_pruning():
    scheduler = Scheduler()
    productive = Source("a", "query", {}, raw=20, novel=20, judged=20, relevant=20, seconds=10, visits=1)
    other = Source("b", "query", {}, raw=20, novel=20, judged=20, relevant=0, seconds=10, visits=1)
    scheduler.add(productive)
    scheduler.add(other)
    assert productive.score(10) > other.score(10)
    for _ in range(30):
        scheduler.batch(productive, 20, 0, 10)
        scheduler.batch(other, 20, 20, 10)
        other.judged += 20
        other.relevant += 20
    assert other.score(100) > productive.score(100)
    assert not productive.exhausted


@pytest.mark.asyncio
async def test_round_plan_keeps_title_and_different_fields_without_lifetime_cap():
    class Gateway:
        async def structured(self, role, payload, schema):
            assert role == "adaptive_search_planner"
            return PortfolioPlan.model_validate({"queries": [
                {"query": "Engineer", "field": "description", "cluster": "core", "evidence": "skills"},
                {"query": " Engineer ", "field": "name", "cluster": "core", "evidence": "title"},
            ]})
    plan = await plan_portfolio(Gateway(), [{"desired_title": "Engineer"}])
    assert {(q["query"], q["field"]) for q in plan} == {("Engineer", "name"), ("Engineer", "description")}
    queries = [{"query": f"role{i}", "field": "name", "cluster": f"role{i}"} for i in range(30)]
    search, _, _ = await controller(queries)
    assert len([s for s in search.scheduler.sources.values() if s.kind == "query"]) == 30


@pytest.mark.asyncio
async def test_checkpoint_continues_page_and_preserves_dedup_and_pending_neighbors():
    search, adapter, page = await controller()
    broad = next(s for s in search.scheduler.sources.values() if s.kind == "coverage")
    adapter.batches[(broad.spec["url"], 0)] = {"refs": refs(1, 3), "terminal": False}
    assert [r.external_id for r in await search.collect_more_job_refs(page)] == ["3"]
    search.related = refs(4)
    restored = AdaptiveSearch(adapter)
    restored.restore_search_checkpoint(json.loads(json.dumps(search.search_checkpoint())))
    assert restored.scheduler.sources[broad.key].page == 1
    assert restored.seen == {"1", "3"}
    assert restored.related == refs(4)
    assert restored.scheduler.checkpoint() == search.scheduler.checkpoint()


@pytest.mark.asyncio
async def test_failing_source_does_not_advance_or_block_healthy_source():
    search, adapter, page = await controller([{"query": "good"}])
    bad = next(s for s in search.scheduler.sources.values() if s.kind == "coverage")
    good = next(s for s in search.scheduler.sources.values() if s.kind == "query")
    adapter.batches[(bad.spec["url"], 0)] = TimeoutError("fixture")
    adapter.batches[(good.spec["url"], 0)] = {"refs": refs(7), "terminal": True}
    search.scheduler.turn = 4
    good.last_turn = 3
    assert await search.collect_more_job_refs(page) == []
    assert bad.page == 0 and not bad.exhausted and bad.failures == 1
    assert [r.external_id for r in await search.collect_more_job_refs(page)] == ["7"]


@pytest.mark.asyncio
async def test_repeated_page_is_not_exhaustion_and_static_epoch_schedules_refresh():
    search, adapter, page = await controller()
    broad = next(s for s in search.scheduler.sources.values() if s.kind == "coverage")
    adapter.batches[(broad.spec["url"], 0)] = {"refs": refs(3), "terminal": False}
    adapter.batches[(broad.spec["url"], 1)] = {"refs": refs(3), "terminal": False}
    await search.collect_more_job_refs(page)
    await search.collect_more_job_refs(page)
    assert broad.page == 1 and not broad.exhausted
    assert not search.search_exhausted
    # An empty epoch is a source boundary, not a completed HH session.
    search, _, page = await controller()
    for _ in range(4):
        await search.collect_more_job_refs(page)
    assert search.search_exhausted
    assert search.next_epoch_refresh_at is not None
    assert search.next_retry_delay() > 0


@pytest.mark.asyncio
async def test_historical_duplicate_page_advances_to_later_novel_page():
    search, adapter, page = await controller()
    broad = next(s for s in search.scheduler.sources.values() if s.kind == "coverage")
    for source in search.scheduler.sources.values():
        if source is not broad:
            source.exhausted = True
    adapter.batches[(broad.spec["url"], 0)] = {"refs": refs(1), "terminal": False}
    adapter.batches[(broad.spec["url"], 1)] = {"refs": refs(3), "terminal": True}
    assert await search.collect_more_job_refs(page) == []
    assert broad.page == 1 and not search.search_exhausted
    assert [r.external_id for r in await search.collect_more_job_refs(page)] == ["3"]


@pytest.mark.asyncio
async def test_empty_nonterminal_page_keeps_source_cursor_open():
    search, adapter, page = await controller()
    broad = next(s for s in search.scheduler.sources.values() if s.kind == "coverage")
    for source in search.scheduler.sources.values():
        if source is not broad:
            source.exhausted = True
    adapter.batches[(broad.spec["url"], 0)] = {"refs": [], "terminal": False}
    adapter.batches[(broad.spec["url"], 1)] = {"refs": refs(9), "terminal": True}
    assert await search.collect_more_job_refs(page) == []
    assert broad.page == 1 and not broad.exhausted and not search.search_exhausted
    assert [r.external_id for r in await search.collect_more_job_refs(page)] == ["9"]


@pytest.mark.asyncio
async def test_epoch_refresh_is_paced_and_checkpointed(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr("backend.orchestrator.adaptive_search.time.time", lambda: now[0])
    search, adapter, page = await controller()
    for _ in range(4):
        await search.collect_more_job_refs(page)
    target = search.next_epoch_refresh_at
    checkpoint = json.loads(json.dumps(search.search_checkpoint()))
    restored = AdaptiveSearch(adapter)
    restored.restore_search_checkpoint(checkpoint)
    assert restored.search_exhausted
    assert restored.seen == search.seen
    assert restored.next_epoch_refresh_at == target
    assert restored.next_retry_delay() == target - now[0]
    assert await restored.collect_more_job_refs(page) == []
    now[0] = target
    for source in restored.scheduler.sources.values():
        adapter.batches[(source.spec["url"], 0)] = {"refs": refs(12), "terminal": False}
    assert [r.external_id for r in await restored.collect_more_job_refs(page)] == ["12"]


@pytest.mark.asyncio
async def test_recovery_control_exceptions_propagate_from_page_reads():
    from backend.orchestrator.recovery import AuthenticationPending, CaptchaRequired

    search, adapter, page = await controller()
    broad = next(s for s in search.scheduler.sources.values() if s.kind == "coverage")
    adapter.batches[(broad.spec["url"], 0)] = CaptchaRequired("captcha")
    with pytest.raises(CaptchaRequired):
        await search.collect_more_job_refs(page)
    adapter.batches[(broad.spec["url"], 0)] = AuthenticationPending("login")
    with pytest.raises(AuthenticationPending):
        await search.collect_more_job_refs(page)


@pytest.mark.asyncio
async def test_source_cooldown_does_not_consume_epoch_refresh_backoff(monkeypatch):
    now = [2000.0]
    monkeypatch.setattr("backend.orchestrator.adaptive_search.time.time", lambda: now[0])
    search, adapter, page = await controller()
    broad = next(s for s in search.scheduler.sources.values() if s.kind == "coverage")
    for source in search.scheduler.sources.values():
        if source is not broad:
            source.exhausted = True
    adapter.batches[(broad.spec["url"], 0)] = TimeoutError("fixture")
    assert await search.collect_more_job_refs(page) == []
    assert await search.collect_more_job_refs(page) == []
    assert search.next_source_retry_at == now[0] + 5
    assert search.next_epoch_refresh_at is None
    now[0] += 5
    adapter.batches[(broad.spec["url"], 0)] = {"refs": [], "terminal": True}
    assert await search.collect_more_job_refs(page) == []
    assert broad.exhausted and not search.search_exhausted
    assert await search.collect_more_job_refs(page) == []
    assert search.search_exhausted
    assert search.next_epoch_refresh_at == now[0] + 5


@pytest.mark.asyncio
async def test_observation_is_idempotent_and_refresh_backoff_requires_activity():
    search, adapter, page = await controller()
    job = SimpleNamespace(external_id="1", title="Engineer", description="Fixture")
    await search.observe(page, job, "apply", 5)
    await search.observe(page, job, "apply", 5)
    assert search.activity == 1 and search.related == refs(2)
    home = search.scheduler.sources[search.home]
    assert home.relevant == home.judged == 1
    search.activity = 20
    await search.collect_more_job_refs(page)
    # Both recommendation lane members get a turn before any repeated home scan.
    for _ in range(4):
        await search.collect_more_job_refs(page)
    assert home.next_refresh == 60
    assert home.exhausted


@pytest.mark.asyncio
async def test_untrusted_restored_source_cannot_navigate_to_external_host():
    search, adapter, _ = await controller()
    data = search.search_checkpoint()
    data["scheduler"]["sources"][0]["spec"]["url"] = "https://evil.example/search/vacancy"
    with pytest.raises(ValueError):
        AdaptiveSearch(adapter).restore_search_checkpoint(data)


@pytest.mark.asyncio
async def test_graph_queue_does_not_starve_coverage():
    search, adapter, page = await controller()
    broad = next(s for s in search.scheduler.sources.values() if s.kind == "coverage")
    adapter.batches[(broad.spec["url"], 0)] = {"refs": refs(9999), "terminal": True}
    search.related = refs(*range(100, 2100))
    found = []
    for _ in range(10):
        found.extend(await search.collect_more_job_refs(page))
    assert "9999" in {r.external_id for r in found}
    assert search.related
