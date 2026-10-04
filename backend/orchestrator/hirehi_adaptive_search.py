"""Independent HireHi adaptive-search orchestration.

Adapter contract (intentionally small and explicit):

``build_source(spec) -> validated spec`` creates a site-specific source and is
the only place allowed to construct a URL; ``open_source(page, spec, cursor)``
opens it; ``collect_card_refs(page)`` returns refs or dictionaries with card
metadata; ``collect_visible_sources(page, context)`` and
``collect_related_refs(page)`` discover more safe source/ref objects.
"""
from __future__ import annotations

import hashlib
import inspect
import json
import re
from difflib import SequenceMatcher
from time import monotonic
from typing import Any
from urllib.parse import urlparse, urlunparse

from pydantic import BaseModel

from backend.adapters.base.protocol import JobRef
from backend.intelligence.hirehi_adaptive_planner import (
    HireHiPortfolio,
    SearchProfile,
    SearchSource,
    criteria_hash,
    plan_hirehi_portfolio,
)
from backend.orchestrator.hirehi_search_scheduler import HireHiSearchScheduler, Source


async def _maybe(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


def _data(value: Any) -> dict[str, Any]:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return dict(value)
    return dict(vars(value))


def _id(ref: Any) -> str:
    data = _data(ref)
    external = str(data.get("external_id") or data.get("id") or "").strip()
    if external:
        return external
    url = str(data.get("url") or "").strip()
    parsed = urlparse(url)
    clean = urlunparse((parsed.scheme.lower(), (parsed.hostname or "").lower(), parsed.path.rstrip("/"), "", parsed.query, ""))
    return clean.casefold()


def _tokens(value: Any) -> set[str]:
    return {x for x in re.findall(r"[\w+#.-]+", str(value or "").casefold()) if len(x) > 1}


class HireHiAdaptiveSearch:
    """Stateful V1--V3 search wrapper; user cancellation is its only normal stop."""

    algorithm_version = "hirehi_adaptive_v3"
    max_active_sources = 12

    def __init__(self, adapter: Any, gateway: Any = None, resumes: list[Any] | None = None,
                 preference_policy: Any = None, *, pro_enabled: bool = False, seed: int = 0,
                 profile: SearchProfile | dict[str, Any] | None = None,
                 criteria_context: dict[str, Any] | None = None,
                 minimum_scores: dict[str, Any] | None = None):
        self.adapter = adapter
        self.gateway = gateway
        self.resumes = resumes or []
        self.preference_policy = preference_policy
        self.pro_enabled = pro_enabled
        self.profile = SearchProfile.model_validate(profile) if profile is not None else SearchProfile()
        self.criteria_context = dict(criteria_context or minimum_scores or {})
        self.scheduler = HireHiSearchScheduler(seed=seed)
        self.portfolio: dict[str, SearchSource | dict[str, Any]] = {}
        self.seen_exact_ids: set[str] = set()
        self.origin_map: dict[str, list[str]] = {}
        self.origins = self.origin_map  # compatibility alias
        self.pending_refs: list[JobRef] = []
        self.card_metadata: dict[str, dict[str, Any]] = {}
        self.verdict_by_id: dict[str, str] = {}
        self.observed_by_source: dict[str, set[str]] = {}
        self.analyzed_ids: set[str] = set()
        self.relevant_ids: set[str] = set()
        self.audit_sample_state: dict[str, Any] = {"eligible": [], "selected": [], "audited_relevant": []}
        self.semantic_clusters: dict[str, dict[str, Any]] = {}
        self.cluster_by_id: dict[str, str] = {}
        self.rejection_reasons: dict[str, int] = {}
        self.relevant_examples: list[dict[str, Any]] = []
        self.analysis_count = 0
        self.next_expansion_at = 50
        self._novelty_expansion_at = 0
        self.next_refresh = 0.0
        self.refresh_backoff = 1.0
        self.search_exhausted = False
        self.last_discovery_batch: dict[str, Any] | None = None
        self.archived_sources: dict[str, dict[str, Any]] = {}
        self._retry_deadline: float | None = None

    def __getattr__(self, name: str) -> Any:
        return getattr(self.adapter, name)

    async def initialize(self) -> HireHiPortfolio:
        portfolio = await plan_hirehi_portfolio(
            self.gateway, self.resumes, self.preference_policy, pro_enabled=self.pro_enabled,
            known_sources=list(self.portfolio), rejection_reasons=self.rejection_reasons,
            relevant_examples=self.relevant_examples,
        )
        self.profile, self.portfolio = portfolio.profile, {}
        for spec in portfolio.sources:
            await self.add_source(spec, origin="planned")
        await self._ensure_minimum_sources()
        return portfolio

    async def _ensure_minimum_sources(self) -> None:
        variants = ("product manager", "product owner", "project manager", "business analyst",
                    "operations manager", "growth", "strategy", "data analyst", "qa", "devops",
                    "marketing", "sales", "designer", "developer", "researcher", "finance",
                    "hr", "customer success", "content", "security")
        index = 0
        while len(self.scheduler.sources) < 8 and index < len(variants):
            await self.add_source(SearchSource(family="query", query=variants[index], cluster="safe-fallback"))
            index += 1

    async def open_search(self, page: Any, filters: dict[str, Any] | None = None) -> None:
        if filters and not self.portfolio:
            # Explicit profile supplied by the user wins over resume/model data.
            self.profile = SearchProfile.model_validate(filters.get("profile", self.profile.model_dump()))
        if not self.portfolio:
            await self.initialize()
        # HireHi's legacy open_search selects one category and is deliberately
        # not called here.  Each source is opened only through open_source.
        home_url = getattr(self.adapter, "home_url", None)
        if home_url and hasattr(page, "goto"):
            await _maybe(page.goto(home_url))

    async def add_source(self, spec: SearchSource | dict[str, Any], *, origin: str = "planned") -> Source | None:
        raw = spec.model_dump(mode="json", exclude_none=True) if isinstance(spec, SearchSource) else dict(spec)
        if raw.get("pro_only") and not self.pro_enabled:
            return None
        # Adapter validation is mandatory.  A model-provided URL is rejected by
        # adapters or ignored; the planner's schema does not expose one.
        try:
            if hasattr(self.adapter, "build_source"):
                validated = await _maybe(self.adapter.build_source(raw))
            elif hasattr(self.adapter, "validate_search_source"):
                validated = self.adapter.validate_search_source(raw)
            else:
                validated = raw
        except (ValueError, TypeError, KeyError):
            return None
        if validated is None:
            return None
        validated = _data(validated)
        visible_url_spec = bool(raw.get("url") and raw.get("kind"))
        if visible_url_spec:
            # URL-bearing specs are trusted only after adapter validation. A
            # model suggestion cannot enter this branch because its schema has
            # no URL/kind fields.
            if not validated.get("url"):
                return None
            family = str(validated.get("kind") or raw.get("kind", ""))
            if family == "similar":
                return None  # related refs are collected from detail pages
            if family not in {"category", "specialization", "coverage"}:
                return None
            key = "visible-" + hashlib.sha256(json.dumps(validated, sort_keys=True, default=str).encode()).hexdigest()[:20]
        else:
            try:
                generated_key = SearchSource.model_validate(raw).key
            except (ValueError, TypeError):
                return None
            key = str(validated.get("source_id") or validated.get("key") or generated_key)
        if key in self.scheduler.sources:
            return self.scheduler.sources[key]
        if len(self.scheduler.sources) >= self.max_active_sources:
            candidates = [s for s in self.scheduler.sources.values()
                          if not s.protected and (s.exhausted or s.cooldown_until is not None)]
            if not candidates and origin == "expansion":
                candidates = [s for s in self.scheduler.sources.values()
                              if not s.protected and s.analyzed >= 25 and s.precision < self.scheduler.quality_target]
            if not candidates and origin in {"related"}:
                candidates = [s for s in self.scheduler.sources.values() if not s.protected]
            if not candidates:
                return None
            victim = min(candidates, key=lambda s: (not s.exhausted, s.analyzed, s.last_used_at or -1, s.key))
            self.archived_sources[victim.key] = victim.stats().model_dump(mode="json")
            self.scheduler.sources.pop(victim.key, None)
            self.portfolio.pop(victim.key, None)
        family = str(validated.get("family") or validated.get("kind") or raw.get("family", "query"))
        cluster = str(raw.get("cluster", ""))
        protected = cluster.casefold() in {"control", "coverage"} or family == "coverage"
        source = Source(key=key, family=family, spec=validated, cluster=cluster,
                        dynamic=family in {"recommendations", "pro_recommendations", "related"}, origin=origin,
                        protected=protected)
        try:
            self.portfolio[key] = SearchSource.model_validate({**raw, "family": family}) if not visible_url_spec else validated
        except (ValueError, TypeError):
            return None
        return self.scheduler.add(source)

    # A synchronous alias is convenient for adapters and unit tests that use
    # a synchronous build_source implementation.
    def add(self, spec: SearchSource | dict[str, Any]) -> Any:
        return self.add_source(spec)

    def _hard_reject_reason(self, ref: Any) -> str | None:
        data = _data(ref)
        text = " ".join(str(data.get(k, "")) for k in ("title", "description", "role", "skills", "company"))
        tokens = _tokens(text)
        for role in self.profile.excluded_roles:
            if _tokens(role) and _tokens(role) <= tokens:
                return "wrong_role"
        for skill in self.profile.excluded_skills:
            if _tokens(skill) and _tokens(skill) <= tokens:
                return "excluded_domain"
        # Missing/unknown fields are deliberately never rejection evidence.
        return None

    def pre_rank(self, refs: list[Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        accepted, rejected = [], []
        for ref in refs:
            data = _data(ref)
            key = _id(data)
            if not key:
                continue
            reason = self._hard_reject_reason(data)
            if reason:
                rejected.append({**data, "external_id": data.get("external_id", key), "rejection_reason": reason})
            else:
                accepted.append({**data, "external_id": data.get("external_id", key)})
        # Stable hash sampling gives exactly reproducible decisions independent
        # of iteration order or process-level random state.
        for item in rejected:
            key = str(item["external_id"])
            digest = int(hashlib.sha256(key.encode()).hexdigest()[:8], 16)
            if key not in self.audit_sample_state["eligible"]:
                self.audit_sample_state["eligible"].append(key)
            if digest % 10 == 0:
                if key not in self.audit_sample_state["selected"]:
                    self.audit_sample_state["selected"].append(key)
                accepted.append(item)
        accepted.sort(key=self._priority, reverse=True)
        return accepted, rejected

    def _priority(self, item: dict[str, Any]) -> tuple[int, int]:
        tokens = _tokens(" ".join(str(item.get(name, "")) for name in ("title", "description", "skills", "company")))
        explicit = sum(1 for value in self.profile.soft_preferences if _tokens(value) <= tokens)
        resume_role = sum(1 for value in self.profile.target_roles + self.profile.adjacent_roles if _tokens(value) <= tokens)
        skill = sum(1 for value in self.profile.required_skills + self.profile.preferred_skills if _tokens(value) <= tokens)
        return explicit * 10000 + resume_role * 100 + skill, -len(str(item.get("external_id", "")))

    @staticmethod
    def _as_job_ref(data: dict[str, Any], key: str) -> JobRef:
        return JobRef(external_id=str(data.get("external_id") or key), url=str(data.get("url") or ""))

    def _record_discovery(self, source: Source, refs: list[Any]) -> list[JobRef]:
        fresh, keys = [], []
        local_seen: set[str] = set()
        for ref in refs:
            item, key = _data(ref), _id(ref)
            if not key:
                continue
            keys.append(key)
            self.card_metadata[key] = item
            if key in self.origin_map:
                source.duplicates += 1
                if source.key not in self.origin_map[key]:
                    self.origin_map[key].append(source.key)
                continue
            self.seen_exact_ids.add(key)
            self.origin_map[key] = [source.key]
            if key not in local_seen:
                fresh.append(item)
                local_seen.add(key)
        accepted, rejected = self.pre_rank(fresh)
        new = accepted
        self.scheduler.update(source, raw=len(refs), unique=len(fresh))
        output = [self._as_job_ref(item, _id(item)) for item in new]
        self.pending_refs.extend(output)
        self.last_discovery_batch = {"source": source.key, "ids": keys, "rejected": len(rejected)}
        return output

    async def collect_card_refs(self, page: Any, source: Source | None = None) -> list[JobRef]:
        if source is None and self._retry_deadline is not None:
            if monotonic() < self._retry_deadline:
                self.search_exhausted = False
                return []
            self.scheduler.advance_to_wakeup()
            self._retry_deadline = None
        source = source or self.scheduler.choose()
        if source is None:
            self.search_exhausted = False
            self.next_refresh = monotonic() + self.scheduler.next_retry_delay()
            self._retry_deadline = self.next_refresh
            return []
        self._retry_deadline = None
        started = monotonic()
        try:
            await _maybe(self.adapter.open_source(page, source.spec, source.cursor))
            result = await _maybe(self.adapter.collect_card_refs(page))
        except Exception:
            self.scheduler.update(source, failures=1)
            source.next_refresh = self.scheduler.turn + min(128, 2 ** min(source.failures + 1, 7))
            return []
        terminal = repeated = unavailable = False
        if isinstance(result, dict):
            refs = result.get("refs", [])
            terminal = bool(result.get("terminal", False))
            repeated = bool(result.get("repeated", False))
            unavailable = bool(result.get("unavailable", False))
        else:
            refs = result
        # The public adapter result carries navigation state while the card
        # contract remains a plain list[JobRef].
        public = None
        getter = getattr(self.adapter, "discovery_result", None)
        if getter is not None:
            public = await _maybe(getter() if callable(getter) else getter)
        elif hasattr(self.adapter, "last_discovery_result"):
            public = self.adapter.last_discovery_result
        if isinstance(public, dict):
            terminal = bool(public.get("terminal", terminal))
            repeated = bool(public.get("repeated", repeated))
            unavailable = bool(public.get("unavailable", unavailable))
            discovered_public = public.get("sources", public.get("discovered_sources", []))
            for spec in discovered_public or []:
                await self.add_source(spec, origin="visible")
        refs = list(refs or [])
        new = self._record_discovery(source, refs)
        if repeated:
            source.consecutive_repeats += 1
            if source.consecutive_repeats >= 3:
                self.scheduler.mark_exhausted(source, refresh_after=max(1, int(self.refresh_backoff)))
        else:
            source.consecutive_repeats = 0
        self.scheduler.update(source, seconds=monotonic() - started, count_batch=False)
        source.cursor = (int(source.cursor) + 1) if isinstance(source.cursor, int) else source.cursor
        if unavailable:
            source.availability = 0.1
            self.scheduler.mark_exhausted(source, refresh_after=min(128, max(1, int(self.refresh_backoff))))
        elif terminal or (not refs and not repeated):
            self.scheduler.mark_exhausted(source, refresh_after=max(1, int(self.refresh_backoff)))
            self.refresh_backoff = min(128.0, self.refresh_backoff * 2)
        else:
            self.refresh_backoff = 1.0
        self.search_exhausted = False
        if hasattr(self.adapter, "collect_visible_sources"):
            try:
                discovered = await _maybe(self.adapter.collect_visible_sources(page, "listing")) or []
            except (ValueError, TypeError, KeyError):
                discovered = []
            for spec in discovered:
                await self.add_source(spec, origin="visible")
        # A navigation call returns at most the scheduler's batch.  Older
        # pending refs are left intact; refs returned by this call are removed
        # from pending so they cannot be returned a second time.
        limit = self.scheduler.batch_size(source)
        returned = new[:limit]
        returned_keys = {_id(ref) for ref in returned}
        self.pending_refs = [ref for ref in self.pending_refs if _id(ref) not in returned_keys]
        return returned

    async def discover_related(self, page: Any, parent_ref: Any = None) -> list[JobRef]:
        """Collect related vacancies from a relevant detail page only."""
        if not hasattr(self.adapter, "collect_related_refs"):
            return []
        try:
            refs = await _maybe(self.adapter.collect_related_refs(page)) or []
        except (ValueError, TypeError, KeyError):
            return []
        related = self.scheduler.sources.get("related")
        if related is None:
            if len(self.scheduler.sources) >= self.max_active_sources:
                candidates = [s for s in self.scheduler.sources.values()
                              if not s.protected and (s.exhausted or s.cooldown_until is not None)]
                if not candidates:
                    candidates = [s for s in self.scheduler.sources.values()
                                  if not s.protected and s.analyzed >= 25 and s.precision < self.scheduler.quality_target]
                if not candidates:
                    return []
                victim = min(candidates, key=lambda s: (not s.exhausted, s.analyzed, s.key))
                self.archived_sources[victim.key] = victim.stats().model_dump(mode="json")
                self.scheduler.sources.pop(victim.key, None)
                self.portfolio.pop(victim.key, None)
            related = self.scheduler.add(Source("related", "related", {"family": "related"}, dynamic=True))
            self.portfolio[related.key] = {"family": "related", "source_id": related.key}
        return self._record_discovery(related, refs) if related is not None else []

    def next_retry_delay(self) -> float:
        """Expose bounded wait for a workflow instead of a busy loop."""
        return min(128.0, max(0.1, self.scheduler.next_retry_delay()))

    async def collect_more(self, page: Any) -> list[JobRef]:
        novelty_trigger = any(source.raw_discovered >= 25 and source.novelty < 0.2
                              for source in self.scheduler.sources.values())
        if self.analysis_count >= self.next_expansion_at or (novelty_trigger and self.analysis_count >= self._novelty_expansion_at):
            await self.expand()
        if self.pending_refs:
            return [self.pending_refs.pop(0)]
        return await self.collect_card_refs(page)

    async def collect_more_job_refs(self, page: Any) -> list[JobRef]:
        return await self.collect_more(page)

    async def collect_job_refs(self, page: Any) -> list[JobRef]:
        """Compatibility alias for the first adaptive discovery batch."""
        return await self.collect_card_refs(page)

    def next_ref(self) -> JobRef | None:
        return self.pending_refs.pop(0) if self.pending_refs else None

    async def observe(self, ref: Any, decision: str | bool = "skip", *legacy: Any,
                      seconds: float = 0.0, reason: str | None = None) -> bool:
        # Older orchestrator call sites passed (page, posting, decision,
        # seconds).  The page is intentionally ignored: the adapter owns all
        # navigation, while accepting both shapes keeps this wrapper isolated.
        detail_page = None
        if legacy:
            if not isinstance(decision, (str, bool)) and len(legacy) >= 1:
                detail_page = ref
                ref, decision = decision, legacy[0]
                if len(legacy) >= 2:
                    seconds = float(legacy[1])
            elif len(legacy) == 1 and isinstance(legacy[0], (int, float)):
                seconds = float(legacy[0])
        data = _data(ref)
        key = _id(data)
        if key in self.analyzed_ids:
            return False
        self.analyzed_ids.add(key)
        self.analysis_count += 1
        relevant = decision is True or str(decision).casefold() in {"apply", "relevant"}
        self.verdict_by_id[key] = "apply" if relevant else "skip"
        if relevant:
            self.relevant_ids.add(key)
            self.relevant_examples.append({"title": str(data.get("title", ""))[:200], "description": str(data.get("description", ""))[:1500]})
            if detail_page is not None:
                await self.discover_related(detail_page, ref)
        else:
            label = reason or "other"
            self.rejection_reasons[label] = self.rejection_reasons.get(label, 0) + 1
        # Attribution is to the first discovery only; all origins are retained
        # for reporting, but an overlap cannot inflate N/R.
        source_keys = self.origin_map.get(key, [])
        for source_key in source_keys[:1]:
            if key in self.observed_by_source.setdefault(source_key, set()):
                continue
            self.observed_by_source[source_key].add(key)
            source = self.scheduler.sources.get(source_key)
            if source:
                self.scheduler.update(source, analyzed=1, relevant=int(relevant), seconds=seconds)
        self._cluster(data, key)
        return True

    async def record_audit_verdict(self, ref: Any, decision: str | bool, *, seconds: float = 0.0) -> bool:
        """Record a full-analysis result for one deterministic audit sample."""
        key = _id(ref)
        if key not in self.audit_sample_state.setdefault("selected", []):
            return False
        relevant = decision is True or str(decision).casefold() in {"apply", "relevant"}
        audited = self.audit_sample_state.setdefault("audited_relevant", [])
        if relevant and key not in audited:
            audited.append(key)
        return await self.observe(ref, decision, seconds=seconds)

    def audit_metrics(self) -> dict[str, Any]:
        selected = self.audit_sample_state.get("selected", [])
        relevant = self.audit_sample_state.get("audited_relevant", [])
        return {"eligible": len(self.audit_sample_state.get("eligible", [])), "selected": len(selected),
                "audited_relevant": len(relevant), "fnr": len(relevant) / len(selected) if selected else None}

    def _cluster(self, data: dict[str, Any], key: str) -> str | None:
        for cluster_id, cluster in self.semantic_clusters.items():
            if self.semantic_near_duplicate(data, cluster.get("representative", {})):
                cluster.setdefault("members", []).append(key)
                self.cluster_by_id[key] = cluster_id
                return cluster_id
        cluster_id = f"cluster-{len(self.semantic_clusters) + 1}"
        self.semantic_clusters[cluster_id] = {"representative": data, "representative_id": key,
                                              "members": [key], "origin": self.origin_map.get(key, [])}
        self.cluster_by_id[key] = cluster_id
        return cluster_id

    def semantic_reuse(self, ref: Any) -> dict[str, Any] | None:
        """Return a verdict only when an analyzed representative is certain."""
        data, key = _data(ref), _id(ref)
        for cluster_id, cluster in self.semantic_clusters.items():
            if key in cluster.get("members", []) or self.semantic_near_duplicate(data, cluster.get("representative", {})):
                representative = str(cluster.get("representative_id", ""))
                if representative in self.analyzed_ids and representative in self.verdict_by_id:
                    return {"decision": self.verdict_by_id[representative], "representative_id": representative,
                            "cluster_id": cluster_id, "reused": True}
        return None

    reuse_verdict = semantic_reuse

    @staticmethod
    def semantic_near_duplicate(left: Any, right: Any) -> bool:
        a, b = _data(left), _data(right)
        company_a, company_b = _clean_company(a.get("company")), _clean_company(b.get("company"))
        if not company_a or not company_b or company_a != company_b:
            return False
        location_a, location_b = _tokens(a.get("location")), _tokens(b.get("location"))
        if location_a and location_b and not (location_a & location_b):
            return False
        title = SequenceMatcher(None, str(a.get("title", "")).casefold(), str(b.get("title", "")).casefold()).ratio()
        desc_a = hashlib.sha256(" ".join(str(a.get("description", "")).casefold().split()).encode()).hexdigest()
        desc_b = hashlib.sha256(" ".join(str(b.get("description", "")).casefold().split()).encode()).hexdigest()
        return title >= 0.86 and desc_a == desc_b

    def metrics(self) -> dict[str, Any]:
        d, n, r = len(self.seen_exact_ids), len(self.analyzed_ids), len(self.relevant_ids)
        audit = self.audit_metrics()
        return {"D": d, "N": n, "R": r, "R/N": r / n if n else 0.0, "N/D": n / d if d else 0.0,
                "audit": self.audit_sample_state, "exploration": self.scheduler.exploration_rounds,
                "exploitation": self.scheduler.exploitation_rounds, "fnr": audit["fnr"],
                "audit_eligible": audit["eligible"], "audit_selected": audit["selected"]}

    async def expand(self) -> list[Source]:
        novelty_trigger = any(source.raw_discovered >= 25 and source.novelty < 0.2
                              for source in self.scheduler.sources.values())
        if self.analysis_count < self.next_expansion_at and not (novelty_trigger and self.analysis_count >= self._novelty_expansion_at):
            return []
        self.next_expansion_at = self.analysis_count + 50
        self._novelty_expansion_at = self.analysis_count + 50
        portfolio = await plan_hirehi_portfolio(self.gateway, self.resumes, self.preference_policy,
            pro_enabled=self.pro_enabled,
            known_sources=[{"spec": source.spec, "stats": source.stats().model_dump(mode="json")}
                           for source in list(self.scheduler.sources.values())[:12]],
            rejection_reasons=self.rejection_reasons,
            relevant_examples=self.relevant_examples)
        added = []
        initial_keys = set(self.scheduler.sources)
        for spec in portfolio.sources:
            source = await self.add_source(spec, origin="expansion")
            if source and source.key not in initial_keys:
                added.append(source)
                initial_keys.add(source.key)
            if len(added) >= 2:
                break
        return added

    def import_weak_priors(self, payload: dict[str, Any]) -> int:
        """Import only weak, compatible cross-session source priors."""
        if payload.get("algorithm_version") != self.algorithm_version:
            return 0
        if payload.get("criteria_hash") != criteria_hash(self.profile, self.criteria_context, self.resumes, self.preference_policy):
            return 0
        applied = 0
        for key, values in (payload.get("sources", {}) or {}).items():
            source = self.scheduler.sources.get(str(key))
            if source and isinstance(values, dict):
                try:
                    successes = float(values.get("successes", 0))
                    failures = float(values.get("failures", 0))
                    if successes < 0 or failures < 0:
                        continue
                except (TypeError, ValueError):
                    continue
                source.prior_successes = min(100.0, successes)
                source.prior_failures = min(100.0, failures)
                applied += 1
        return applied

    def search_checkpoint(self) -> dict[str, Any]:
        return {
            "algorithm_version": self.algorithm_version,
            "criteria_hash": criteria_hash(self.profile, self.criteria_context, self.resumes, self.preference_policy),
            "profile_snapshot": self.profile.model_dump(mode="json"),
            "portfolio": [
                s.model_dump(mode="json", exclude_none=True) if isinstance(s, SearchSource) else s
                for s in self.portfolio.values()
            ],
            "source_stats": {key: source.stats().model_dump(mode="json") for key, source in self.scheduler.sources.items()},
            "source_cursors": {key: source.cursor for key, source in self.scheduler.sources.items()},
            "pending_refs": [ref.model_dump(mode="json") for ref in self.pending_refs], "seen_exact_ids": sorted(self.seen_exact_ids),
            "semantic_clusters": self.semantic_clusters, "origin_map": self.origin_map,
            "analyzed_ids": sorted(self.analyzed_ids), "relevant_ids": sorted(self.relevant_ids),
            "audit_sample_state": self.audit_sample_state, "exploration_rng_state": self.scheduler.checkpoint()["rng_state"],
            "next_expansion_at": self.next_expansion_at,
            "budgets": {"analysis_count": self.analysis_count, "novelty_expansion_at": self._novelty_expansion_at},
            "scheduler": self.scheduler.checkpoint(), "verdict_by_id": self.verdict_by_id,
            "observed_by_source": {key: sorted(value) for key, value in self.observed_by_source.items()},
            "archived_sources": self.archived_sources, "search_exhausted": self.search_exhausted,
            "rejection_reasons": self.rejection_reasons,
            "relevant_examples": self.relevant_examples[-12:],
        }

    async def restore_search_checkpoint(self, data: dict[str, Any]) -> None:
        if data.get("algorithm_version") != self.algorithm_version:
            raise ValueError("incompatible HireHi adaptive algorithm version")
        profile = SearchProfile.model_validate(data.get("profile_snapshot", {}))
        expected = criteria_hash(profile, self.criteria_context, self.resumes, self.preference_policy)
        if data.get("criteria_hash") != expected:
            raise ValueError("incompatible HireHi criteria hash")

        async def adapter_canonicalize(raw_spec: Any) -> dict[str, Any]:
            if not isinstance(raw_spec, dict):
                raise ValueError("invalid HireHi checkpoint source")
            if hasattr(self.adapter, "build_source"):
                canonical = await _maybe(self.adapter.build_source(raw_spec))
                if canonical is None:
                    raise ValueError("adapter rejected checkpoint source")
                return _data(canonical)
            validator = getattr(self.adapter, "validate_search_source", None)
            if validator:
                canonical = await _maybe(validator(raw_spec))
                if canonical is False:
                    raise ValueError("adapter rejected checkpoint source")
                if isinstance(canonical, dict):
                    return _data(canonical)
            return dict(raw_spec)

        async def canonicalize_portfolio_source(raw_spec: Any) -> tuple[dict[str, Any], bool]:
            """Validate legacy planner specs before handing them to the adapter."""
            if not isinstance(raw_spec, dict):
                raise ValueError("invalid HireHi checkpoint source")
            visible_url_spec = bool(raw_spec.get("url") and raw_spec.get("kind"))
            # Related is an internal adapter-created arm, not a planner
            # SearchSource (its family is intentionally outside that schema).
            if visible_url_spec or raw_spec.get("family") == "related":
                spec = dict(raw_spec)
            else:
                try:
                    # SearchSource is intentionally strict.  In particular,
                    # unknown filter keys and invalid non-null values must
                    # remain hard restore failures.
                    spec = SearchSource.model_validate(raw_spec).model_dump(
                        mode="json", exclude_none=True
                    )
                except (TypeError, ValueError) as exc:
                    raise ValueError("invalid HireHi planner source") from exc
            return await adapter_canonicalize(spec), visible_url_spec

        scheduler_data = data.get("scheduler")
        if scheduler_data is None:
            scheduler_data = {"sources": [], "rng_state": data.get("exploration_rng_state")}
            for key, stats in data.get("source_stats", {}).items():
                spec = next((x for x in data.get("portfolio", []) if x.get("source_id") == key or x.get("key") == key), {"source_id": key})
                scheduler_data["sources"].append({
                    "key": key, "family": stats.get("family", spec.get("family", "query")), "spec": spec,
                    "cursor": data.get("source_cursors", {}).get(key, stats.get("cursor", 0)),
                    "exhausted": stats.get("exhausted", False), "raw_discovered": stats.get("raw_discovered", 0),
                    "unique_discovered": stats.get("unique_discovered", 0), "analyzed": stats.get("analyzed", 0),
                    "relevant": stats.get("relevant", 0), "duplicates": stats.get("duplicates", 0),
                    "failures": stats.get("failures", 0), "elapsed_seconds": stats.get("elapsed_seconds", 0),
                    "cooldown_until": stats.get("cooldown_until"), "last_used_at": stats.get("last_used_at"),
                })
        restored = HireHiSearchScheduler.restore(scheduler_data)
        for source in restored.sources.values():
            # Scheduler specs are already adapter-canonical (and may contain
            # URL/source_id fields not accepted by SearchSource).  Preserve
            # the original direct adapter validation path here.
            source.spec = await adapter_canonicalize(source.spec)
        for raw_spec in data.get("portfolio", []):
            await canonicalize_portfolio_source(raw_spec)
        self.profile = profile
        self.scheduler = restored
        self.portfolio = {source.key: source.spec for source in restored.sources.values()}
        self.pending_refs = [self._as_job_ref(_data(ref), _id(ref)) for ref in data.get("pending_refs", [])]
        self.seen_exact_ids = set(data.get("seen_exact_ids", []))
        self.origin_map = {str(k): list(v) for k, v in data.get("origin_map", {}).items()}
        self.origins = self.origin_map
        self.semantic_clusters = dict(data.get("semantic_clusters", {}))
        self.cluster_by_id = {member: cluster_id for cluster_id, cluster in self.semantic_clusters.items()
                              for member in cluster.get("members", [])}
        self.analyzed_ids, self.relevant_ids = set(data.get("analyzed_ids", [])), set(data.get("relevant_ids", []))
        self.audit_sample_state = dict(data.get("audit_sample_state", {"eligible": [], "selected": []}))
        self.verdict_by_id = {str(key): str(value) for key, value in data.get("verdict_by_id", {}).items()}
        self.observed_by_source = {str(key): set(value) for key, value in data.get("observed_by_source", {}).items()}
        self.archived_sources = dict(data.get("archived_sources", {}))
        reasons = data.get("rejection_reasons", {})
        if not isinstance(reasons, dict) or any(not isinstance(key, str) or not isinstance(value, int) or value < 0 for key, value in reasons.items()):
            raise ValueError("invalid rejection reasons checkpoint")
        examples = data.get("relevant_examples", [])
        if not isinstance(examples, list) or any(not isinstance(item, dict) for item in examples):
            raise ValueError("invalid relevant examples checkpoint")
        self.rejection_reasons = dict(reasons)
        self.relevant_examples = [{"title": str(item.get("title", ""))[:200], "description": str(item.get("description", ""))[:1500]}
                                  for item in examples[-12:]]
        self.next_expansion_at = int(data.get("next_expansion_at", 50))
        self.analysis_count = int(data.get("budgets", {}).get("analysis_count", len(self.analyzed_ids)))
        self._novelty_expansion_at = int(data.get("budgets", {}).get("novelty_expansion_at", 0))
        self.search_exhausted = bool(data.get("search_exhausted", False))

    restore_checkpoint = restore_search_checkpoint


def _clean_company(value: Any) -> str:
    return " ".join(str(value or "").casefold().split())


AdaptiveSearch = HireHiAdaptiveSearch
__all__ = ["HireHiAdaptiveSearch", "AdaptiveSearch"]
