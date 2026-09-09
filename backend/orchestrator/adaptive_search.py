"""Durable HH portfolio; site operations stay in the adapter.

A session covers a finite epoch of observed sources. Refreshes require new
activity and back off when stale; exhaustion never means all of HH was searched.
"""
import asyncio
from time import perf_counter
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from backend.adapters.base.protocol import JobRef
from backend.intelligence.adaptive_search_planner import plan_portfolio
from backend.intelligence.gateway import ModelUnavailable
from backend.orchestrator.search_scheduler import Scheduler, Source
from backend.services.search_metrics import fingerprint, record


class AdaptiveSearch:
    def __init__(self, adapter, gateway=None, resumes=None, policy=None):
        self.adapter = adapter
        self.gateway, self.resumes, self.policy = gateway, resumes or [], policy
        self.scheduler = Scheduler()
        self.seen = set()
        self.origins = {}
        self.observed = set()
        self.overlaps = set()
        self.related = []
        self.examples = []
        self.activity = 0
        self.next_expansion = 50
        self.search_exhausted = False
        self.last_discovery_batch = None

    def __getattr__(self, name):
        return getattr(self.adapter, name)

    def add(self, spec):
        self.adapter.validate_search_source(spec)
        parsed = urlparse(spec["url"])
        params = {k: v for k, v in parse_qsl(parsed.query)
                  if k not in {"page", "hhtmFrom", "hhtmFromLabel", "from", "source"}}
        url = urlunparse(parsed._replace(query=urlencode(sorted(params.items())), fragment=""))
        key = fingerprint(url)[:20]
        self.scheduler.add(Source(key=key, kind=spec["kind"], spec={**spec, "url": url}, cluster=spec.get("cluster", "")))
        return self.scheduler.sources[key]

    async def open_search(self, page, filters):
        await self.adapter.open_search(page, {})
        self.home = self.add({"url": self.adapter.home_url, "kind": "recommendations", "cluster": "home"}).key
        for query in filters.get("portfolio_queries", []):
            self.add(self.adapter.query_source(query["query"], query.get("field", "name"), query.get("cluster", "")))
        self.add(self.adapter.query_source(""))

    def accept(self, source, refs, seconds):
        new = [ref for ref in refs if ref.external_id not in self.seen]
        # Exact IDs only. Similar titles/companies remain distinct vacancies.
        unique = {ref.external_id: ref for ref in new}
        for key in unique:
            self.origins[key] = source.key
        self.seen.update(unique)
        self.scheduler.batch(source, len(refs), len(unique), seconds)
        self.last_discovery_batch = {"source": f"{source.kind}:{source.key}", "cluster": source.cluster,
                                     "page": source.page, "ids": [ref.external_id for ref in refs]}
        return list(unique.values())

    async def collect_job_refs(self, page):
        started = perf_counter()
        refs = await self.adapter.collect_job_refs(page)
        home = self.scheduler.sources[self.home]
        home.exhausted = True
        home.next_refresh = 20
        for spec in await self.adapter.collect_visible_sources(page):
            self.add(spec)
        if urlparse(page.url).path == "/search/vacancy":
            listing = self.add({"url": page.url, "kind": "recommendations", "cluster": "home"})
            # The home snapshot already consumed page zero of this listing.
            listing.page = 1 if refs else 0
            listing.exhausted = bool(refs) and await self.adapter.discovery_listing_terminal(page)
            listing.next_refresh = 20
            if refs:
                listing.signatures = [fingerprint([r.external_id for r in refs])]
        return self.accept(home, refs, perf_counter() - started)

    async def collect_more_job_refs(self, page):
        if self.related:
            source = self.scheduler.sources.setdefault("related", Source("related", "related", {}))
            source.exhausted = False
        for source in self.scheduler.sources.values():
            if source.kind == "recommendations" and source.exhausted and self.activity >= source.next_refresh:
                source.exhausted = False
                source.page = 0
                source.signatures = []
        await self.expand()
        source = self.scheduler.choose()
        if source is None:
            self.search_exhausted = True
            self.last_discovery_batch = {"source": "epoch_complete", "ids": []}
            return []
        if source.key == "related":
            refs, self.related = self.related[:20], self.related[20:]
            source.exhausted = not self.related
            return self.accept(source, refs, 0)
        started = perf_counter()
        try:
            if source.failures:
                await asyncio.sleep(min(30, 2 ** min(source.failures, 5)))
            result = await self.adapter.read_discovery_page(page, source.spec, source.page)
            refs = result["refs"]
            signature = fingerprint([ref.external_id for ref in refs]) if refs else None
            if signature and signature in source.signatures:
                raise RuntimeError("HH повторил страницу источника")
        except Exception as exc:
            source.failures += 1
            source.retry_after = self.scheduler.turn + 10
            self.last_discovery_batch = {"source": f"{source.kind}:{source.key}", "ids": [], "failed": True}
            record("search_source_error", {"source": source.key, "page": source.page, "error": type(exc).__name__})
            return []
        source.failures = 0
        new = self.accept(source, refs, perf_counter() - started)
        if signature:
            source.signatures = [*source.signatures[-2:], signature]
        source.page += 1
        source.exhausted = result["terminal"]
        if source.kind == "recommendations":
            source.stale_refreshes = 0 if new else source.stale_refreshes + 1
            source.next_refresh = self.activity + min(320, 20 * 2 ** min(source.stale_refreshes, 4))
        for spec in result.get("sources", []):
            self.add(spec)
        record("search_choice", {"source": source.key, "kind": source.kind, "new": len(new),
                                  "raw": len(refs), "turn": self.scheduler.turn})
        return new

    def observe_overlap(self, external_id):
        if external_id not in self.overlaps:
            self.overlaps.add(external_id)
            source = self.scheduler.sources.get(self.origins.get(external_id))
            if source:
                source.novel = max(0, source.novel - 1)

    async def observe(self, page, posting, decision, seconds):
        key = posting.external_id
        if key in self.observed:
            return
        # Read neighbors before marking the observation durable; failed reads
        # may retry without silently losing the source. No application actions.
        if decision == "apply":
            for spec in await self.adapter.collect_visible_sources(page, context="relevant"):
                self.add(spec)
            related = await self.adapter.collect_related_refs(page)
            record("discovery", {"source": "related:related", "ids": [r.external_id for r in related]})
            queued = {r.external_id: r for r in self.related}
            queued.update({r.external_id: r for r in related if r.external_id not in self.seen})
            self.related = list(queued.values())
            self.examples = [*self.examples[-11:], {"title": posting.title, "description": posting.description[:1500]}]
        source = self.scheduler.sources.get(self.origins.get(key))
        if source:
            source.seconds += max(0, seconds)
            if decision in {"apply", "skip"}:
                source.judged += 1
                source.relevant += decision == "apply"
        self.observed.add(key)
        self.activity += 1

    async def expand(self):
        if not self.gateway or self.activity < self.next_expansion or not self.examples:
            return
        self.next_expansion = self.activity + 50
        known = [dict(parse_qsl(urlparse(s.spec["url"]).query)).get("text", "")
                 for s in self.scheduler.sources.values() if s.kind == "query"]
        try:
            queries = await plan_portfolio(self.gateway, self.resumes, self.policy, known=known, relevant=self.examples)
        except (ModelUnavailable, ValueError):
            # Optional expansion failure cannot stop existing usable sources.
            record("expansion_error", {"activity": self.activity})
            return
        for query in queries:
            self.add(self.adapter.query_source(query["query"], query["field"], query["cluster"]))

    def search_checkpoint(self):
        return {"algorithm": "adaptive_v1", "scheduler": self.scheduler.checkpoint(),
                "seen": sorted(self.seen), "origins": self.origins, "observed": sorted(self.observed),
                "overlaps": sorted(self.overlaps), "related": [r.model_dump() for r in self.related],
                "examples": self.examples, "activity": self.activity, "next_expansion": self.next_expansion,
                "home": self.home, "exhausted": self.search_exhausted}

    def restore_search_checkpoint(self, data):
        if data.get("algorithm") != "adaptive_v1":
            raise ValueError("Сессия создана другой версией поиска; продолжите её в исходной ветке")
        scheduler = Scheduler.restore(data["scheduler"])
        for source in scheduler.sources.values():
            if source.key != "related":
                self.adapter.validate_search_source(source.spec)
        self.scheduler = scheduler
        self.seen, self.observed, self.overlaps = set(data["seen"]), set(data["observed"]), set(data["overlaps"])
        self.origins = dict(data["origins"])
        self.related = [JobRef.model_validate(r) for r in data["related"]]
        self.examples, self.activity = data["examples"], data["activity"]
        self.next_expansion, self.home = data["next_expansion"], data["home"]
        self.search_exhausted = data["exhausted"]
