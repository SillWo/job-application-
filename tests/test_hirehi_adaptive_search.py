"""Contract tests for the isolated HireHi adaptive engine."""
from __future__ import annotations

import copy
import json

import pytest
from pydantic import ValidationError

from backend.adapters.base.protocol import JobRef
from backend.intelligence.hirehi_adaptive_planner import (
    HireHiSourceFilters,
    SearchProfile,
    SearchSource,
    deterministic_profile,
)
from backend.orchestrator.hirehi_adaptive_search import HireHiAdaptiveSearch
from backend.orchestrator.hirehi_search_scheduler import HireHiSearchScheduler, Source
from backend.schemas.domain import (
    ResumeLanguage,
    ResumeLocation,
    ResumeProfessionalView,
    ResumeSkill,
    ResumeTarget,
    SourceField,
)


class Adapter:
    def build_source(self, spec):
        if "url" in spec and not str(spec["url"]).startswith("https://hirehi.ru/"):
            raise ValueError("unsafe URL")
        return {**spec, "source_id": spec.get("source_id", spec.get("query", spec.get("family")))}


def test_profile_normalizes_and_explicit_policy_wins():
    profile = deterministic_profile([{"desired_title": " Product Manager ", "skills": ["SQL"]}], {"target_roles": ["Owner"]})
    assert profile.target_roles == ["Owner"]
    assert profile.preferred_skills == ["SQL"]
    with pytest.raises(ValidationError):
        SearchProfile.model_validate({"unexpected": True})


def test_unknown_card_is_not_rejected_and_audit_is_reproducible():
    card = {"external_id": "unknown", "url": "https://hirehi.ru/vacancy/1"}
    left = HireHiAdaptiveSearch(Adapter(), profile={"excluded_roles": ["intern"]})
    right = HireHiAdaptiveSearch(Adapter(), profile={"excluded_roles": ["intern"]})
    assert left.pre_rank([card])[0] == right.pre_rank([card])[0]
    assert not left.audit_sample_state["selected"]


def test_scheduler_has_exact_twenty_percent_exploration_lane():
    scheduler = HireHiSearchScheduler(seed=2)
    for i in range(3):
        scheduler.add(Source(str(i), "query", {"query": str(i)}))
    for _ in range(50):
        scheduler.choose()
    assert scheduler.exploration_rounds == 10
    assert scheduler.exploitation_rounds == 40


def test_thompson_updates_and_cooldown_reactivation():
    scheduler = HireHiSearchScheduler(seed=1, quality_target=0.2)
    source = scheduler.add(Source("weak", "query", {}))
    scheduler.update(source, analyzed=25, relevant=0)
    assert source.cooldown_until is not None
    scheduler.turn = source.cooldown_until
    assert scheduler.choose() is not None


@pytest.mark.asyncio
async def test_checkpoint_validates_adapter_and_preserves_rng():
    adapter = Adapter()
    resumes = [{"desired_title": "PM", "skills": ["SQL"]}]
    policy = {"red_flags": [{"text": "gambling"}]}
    criteria_context = {"minimum_score": 70}
    search = HireHiAdaptiveSearch(
        adapter,
        resumes=resumes,
        preference_policy=policy,
        criteria_context=criteria_context,
        profile={"target_roles": ["PM"]},
        seed=4,
    )
    source = await search.add_source(SearchSource(family="query", query="PM"))
    source.raw_discovered = 3
    source.unique_discovered = 2
    source.analyzed = 2
    source.relevant = 1
    search.pending_refs = [JobRef(external_id="pending", url="https://hirehi.ru/vacancy/pending")]
    search.seen_exact_ids.add("1")
    search.analyzed_ids.add("analyzed")
    search.relevant_ids.add("relevant")
    # The planner may enrich the deterministic profile before checkpointing.
    search.profile = SearchProfile(
        target_roles=["PM"], adjacent_roles=["Product Owner"], soft_preferences=["SaaS"]
    )
    checkpoint = search.search_checkpoint()
    restored = HireHiAdaptiveSearch(
        adapter,
        resumes=resumes,
        preference_policy=policy,
        criteria_context=criteria_context,
        profile={"target_roles": ["PM"]},
    )
    await restored.restore_search_checkpoint(copy.deepcopy(checkpoint))
    assert restored.seen_exact_ids == {"1"}
    assert restored.profile == search.profile
    assert restored.pending_refs == search.pending_refs
    restored_source = restored.scheduler.sources[next(iter(restored.scheduler.sources))]
    assert (restored_source.raw_discovered, restored_source.unique_discovered,
            restored_source.analyzed, restored_source.relevant) == (3, 2, 2, 1)
    assert restored.analyzed_ids == {"analyzed"}
    assert restored.relevant_ids == {"relevant"}
    assert restored.scheduler.checkpoint()["rng_state"] == search.scheduler.checkpoint()["rng_state"]


@pytest.mark.asyncio
async def test_restore_canonicalizes_legacy_null_filter_fields():
    search = HireHiAdaptiveSearch(Adapter(), profile={"target_roles": ["PM"]})
    await search.add_source(SearchSource(family="query", query="PM"))
    checkpoint = search.search_checkpoint()
    legacy_filters = {
        "english": None,
        "salary_to": None,
        "level": [],
        "format": [],
        "region": [],
        "direct_contact": [],
    }
    checkpoint["portfolio"][0]["filters"] = legacy_filters

    restored = HireHiAdaptiveSearch(Adapter())
    await restored.restore_search_checkpoint(copy.deepcopy(checkpoint))
    assert restored.portfolio
    assert all(
        value is not None
        for spec in restored.portfolio.values()
        for value in spec.get("filters", {}).values()
    )
    round_trip = restored.search_checkpoint()
    assert all(
        value is not None
        for spec in round_trip["portfolio"]
        for value in spec.get("filters", {}).values()
    )
    assert "english" not in round_trip["portfolio"][0]["filters"]
    assert "salary_to" not in round_trip["portfolio"][0]["filters"]


@pytest.mark.asyncio
async def test_restore_accepts_canonical_scheduler_spec_without_kind():
    class CanonicalAdapter(Adapter):
        def build_source(self, spec):
            raw = super().build_source(spec)
            return {**raw, "url": "https://hirehi.ru/?search=PM", "source_id": "canonical-id"}

    adapter = CanonicalAdapter()
    search = HireHiAdaptiveSearch(adapter, profile={"target_roles": ["PM"]})
    await search.add_source(SearchSource(family="query", query="PM"))
    checkpoint = search.search_checkpoint()
    scheduler_spec = checkpoint["scheduler"]["sources"][0]["spec"]
    assert scheduler_spec["url"] and scheduler_spec["source_id"] == "canonical-id"
    assert "kind" not in scheduler_spec

    restored = HireHiAdaptiveSearch(CanonicalAdapter())
    await restored.restore_search_checkpoint(copy.deepcopy(checkpoint))
    assert restored.portfolio
    assert restored.portfolio["canonical-id"]["url"] == "https://hirehi.ru/?search=PM"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_filter",
    [{"unknown": "x"}, {"english": "sometimes"}],
)
async def test_restore_rejects_unknown_or_invalid_non_null_filter(bad_filter):
    search = HireHiAdaptiveSearch(Adapter(), profile={"target_roles": ["PM"]})
    await search.add_source(SearchSource(family="query", query="PM"))
    checkpoint = search.search_checkpoint()
    checkpoint["portfolio"][0]["filters"] = bad_filter
    with pytest.raises(ValueError):
        await HireHiAdaptiveSearch(Adapter()).restore_search_checkpoint(
            copy.deepcopy(checkpoint)
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["resume", "policy", "context"])
async def test_checkpoint_rejects_changed_current_criteria(changed):
    adapter = Adapter()
    resumes = [{"desired_title": "PM"}]
    policy = {"red_flags": [{"text": "gambling"}]}
    criteria_context = {"minimum_score": 70}
    search = HireHiAdaptiveSearch(
        adapter,
        resumes=resumes,
        preference_policy=policy,
        criteria_context=criteria_context,
        profile={"target_roles": ["PM"], "adjacent_roles": ["Product Owner"]},
    )
    await search.add_source(SearchSource(family="query", query="PM"))
    checkpoint = search.search_checkpoint()
    changed_resumes = [{"desired_title": "Data Engineer"}]
    changed_policy = {"red_flags": [{"text": "crypto"}]}
    changed_context = {"minimum_score": 80}
    restored = HireHiAdaptiveSearch(
        adapter,
        resumes=changed_resumes if changed == "resume" else resumes,
        preference_policy=changed_policy if changed == "policy" else policy,
        criteria_context=changed_context if changed == "context" else criteria_context,
        profile={"target_roles": ["different deterministic role"]},
    )
    with pytest.raises(ValueError, match="criteria hash"):
        await restored.restore_search_checkpoint(copy.deepcopy(checkpoint))


def test_conservative_semantic_cluster_requires_same_company_and_description():
    a = {"company": "Acme", "location": "Moscow", "title": "Product Manager", "description": "same"}
    b = {"company": "Acme", "location": "Moscow", "title": "Product Manager", "description": "same"}
    c = {"company": "Other", "location": "Moscow", "title": "Product Manager", "description": "same"}
    assert HireHiAdaptiveSearch.semantic_near_duplicate(a, b)
    assert not HireHiAdaptiveSearch.semantic_near_duplicate(a, c)


def test_domain_resume_shape_and_policy_flags_are_extracted_without_exclusions():
    resume = ResumeProfessionalView(
        target=ResumeTarget(desired_title=SourceField(value="PM"), specializations=SourceField(value=["Product"])),
        location=ResumeLocation(residence=SourceField(value="Moscow")),
        skills=[ResumeSkill(name=SourceField(value="SQL"))],
        languages=[ResumeLanguage(language=SourceField(value="English"))],
    )
    profile = deterministic_profile([resume], {"red_flags": [{"text": "gambling", "category": "other"}], "green_flags": [{"text": "SaaS", "category": "other"}], "desired_salary": {"minimum": 100000}})
    assert {"PM", "Product"} <= set(profile.target_roles)
    assert profile.preferred_skills == ["SQL"]
    assert profile.locations == ["Moscow"]
    assert profile.hard_constraints == ["gambling"]
    assert profile.soft_preferences == ["SaaS"]
    assert profile.salary_floor == 100000
    assert profile.excluded_roles == [] and profile.excluded_skills == []


def test_source_filters_are_strict_and_profile_maps_visible_filters():
    with pytest.raises(ValidationError):
        HireHiSourceFilters.model_validate({"level": "middle", "unknown": "x"})
    with pytest.raises(ValidationError):
        HireHiSourceFilters.model_validate({"level": "not-a-grade"})
    profile = deterministic_profile([{"desired_title": "PM", "grades": ["middle"], "work_formats": ["Удалённо"]}], {"desired_salary": {"minimum": 100000}})
    assert profile.remote_policy == "удалённо"
    import asyncio
    portfolio = asyncio.run(__import__("backend.intelligence.hirehi_adaptive_planner", fromlist=["plan_hirehi_portfolio"]).plan_hirehi_portfolio(None, [{"desired_title": "PM", "grades": ["middle"], "work_formats": ["Удалённо"]}], {"desired_salary": {"minimum": 100000}}))
    assert portfolio.sources[0].filters.level == ["middle"]
    assert portfolio.sources[0].filters.format == ["удалённо"]
    assert portfolio.sources[0].filters.salary_from == 100000


def test_visible_adapter_url_shape_is_trusted_only_after_validation():
    import asyncio
    search = HireHiAdaptiveSearch(Adapter())
    visible = {"url": "https://hirehi.ru/vacancies/product", "kind": "category", "cluster": "visible"}
    source = asyncio.run(search.add_source(visible))
    assert source is not None and source.spec["url"].startswith("https://hirehi.ru/")
    assert asyncio.run(search.add_source({"url": "https://evil.example/", "kind": "category"})) is None


def test_salary_float_is_safe_integer_filter_and_invalid_values_are_ignored():
    import asyncio
    good = asyncio.run(__import__("backend.intelligence.hirehi_adaptive_planner", fromlist=["plan_hirehi_portfolio"]).plan_hirehi_portfolio(
        None, [{"desired_title": "PM"}], {"desired_salary": {"minimum": 150000.0}}))
    assert good.sources[0].filters.salary_from == 150000
    assert SearchProfile(salary_floor=float("nan")).salary_floor is None


def test_region_filters_are_strict_and_location_mapping_is_conservative():
    import asyncio

    from backend.intelligence.hirehi_adaptive_planner import HireHiSourceFilters
    with pytest.raises(ValidationError):
        HireHiSourceFilters.model_validate({"region": ["Asia"]})
    portfolio = asyncio.run(__import__("backend.intelligence.hirehi_adaptive_planner", fromlist=["plan_hirehi_portfolio"]).plan_hirehi_portfolio(
        None, [{"desired_title": "PM", "locations": ["Россия, Москва"]}]))
    assert portfolio.sources[0].filters.region == ["Russia"]


def test_empty_resume_still_gets_eight_safe_sources():
    import asyncio
    portfolio = asyncio.run(__import__("backend.intelligence.hirehi_adaptive_planner", fromlist=["plan_hirehi_portfolio"]).plan_hirehi_portfolio(None, []))
    assert 8 <= len(portfolio.sources) <= 12
    assert all("url" not in source.model_dump() for source in portfolio.sources)


def test_rng_checkpoint_is_json_safe_and_rejects_invalid_shape():
    scheduler = HireHiSearchScheduler(seed=2)
    payload = scheduler.checkpoint()
    json.dumps(payload)
    bad = dict(payload)
    bad["rng_state"] = {"not": "random"}
    with pytest.raises(ValueError):
        HireHiSearchScheduler.restore(bad)


def test_scheduler_waits_without_turn_jump_then_reactivates_once():
    scheduler = HireHiSearchScheduler(seed=1)
    source = scheduler.add(Source("e", "query", {}, exhausted=True, next_refresh=3))
    assert scheduler.choose() is None and scheduler.turn == 0
    assert scheduler.next_retry_delay() == 3
    assert scheduler.advance_to_wakeup() and scheduler.turn == 3
    assert scheduler.choose() is source


def test_weak_prior_changes_posterior_without_session_telemetry():
    scheduler = HireHiSearchScheduler(seed=1)
    source = scheduler.add(Source("p", "query", {}, prior_successes=4, prior_failures=1))
    assert source.posterior == (5, 2)
    assert source.stats().analyzed == 0 and source.stats().relevant == 0


@pytest.mark.asyncio
async def test_async_restore_validates_every_source_and_keeps_real_keys():
    class AsyncAdapter(Adapter):
        async def build_source(self, spec):
            if spec.get("url", "").startswith("https://evil"):
                raise ValueError("unsafe")
            return spec
    search = HireHiAdaptiveSearch(AsyncAdapter(), profile={"target_roles": ["PM"]})
    await search.add_source(SearchSource(family="query", query="PM"))
    checkpoint = search.search_checkpoint()
    restored = HireHiAdaptiveSearch(AsyncAdapter())
    await restored.restore_search_checkpoint(json.loads(json.dumps(checkpoint)))
    assert set(restored.portfolio) == set(restored.scheduler.sources)


@pytest.mark.asyncio
async def test_async_restore_rejects_malicious_adapter_source():
    search = HireHiAdaptiveSearch(Adapter(), profile={"target_roles": ["PM"]})
    await search.add_source(SearchSource(family="query", query="PM"))
    checkpoint = search.search_checkpoint()
    checkpoint["scheduler"]["sources"][0]["spec"]["url"] = "https://evil.example/"
    with pytest.raises(ValueError):
        await HireHiAdaptiveSearch(Adapter()).restore_search_checkpoint(checkpoint)


def test_all_exact_cards_count_discovery_and_rejected_overlap_keeps_origins():
    from backend.orchestrator.hirehi_search_scheduler import Source
    search = HireHiAdaptiveSearch(Adapter(), profile={"excluded_roles": ["intern"]})
    first, second = Source("a", "query", {}), Source("b", "query", {})
    card = {"external_id": "x", "url": "https://hirehi.ru/x", "title": "Intern", "description": ""}
    assert search._record_discovery(first, [card]) == []
    assert search._record_discovery(second, [card]) == []
    assert search.seen_exact_ids == {"x"}
    assert search.origin_map["x"] == ["a", "b"]
    assert first.unique_discovered == 1 and second.unique_discovered == 0


def test_priority_places_explicit_soft_then_resume_match_before_unknown():
    search = HireHiAdaptiveSearch(Adapter(), profile={"target_roles": ["PM"], "soft_preferences": ["SaaS"]})
    refs = [
        {"external_id": "unknown", "title": "Other", "url": "https://hirehi.ru/1"},
        {"external_id": "role", "title": "PM", "url": "https://hirehi.ru/2"},
        {"external_id": "soft", "title": "PM SaaS", "url": "https://hirehi.ru/3"},
    ]
    accepted, _ = search.pre_rank(refs)
    assert [item["external_id"] for item in accepted] == ["soft", "role", "unknown"]


@pytest.mark.asyncio
async def test_related_is_collected_only_from_relevant_detail_legacy_call():
    class RelatedAdapter(Adapter):
        def __init__(self):
            self.calls = 0
        async def collect_related_refs(self, page):
            self.calls += 1
            return [{"external_id": "related", "url": "https://hirehi.ru/r"}]
    adapter = RelatedAdapter()
    search = HireHiAdaptiveSearch(adapter, profile={"target_roles": ["PM"]})
    detail = object()
    await search.observe(detail, {"external_id": "p", "url": "https://hirehi.ru/p", "title": "PM"}, "apply", 0)
    assert adapter.calls == 1 and "related" in search.seen_exact_ids
    await search.observe(detail, {"external_id": "q", "url": "https://hirehi.ru/q"}, "skip", 0)
    assert adapter.calls == 1


@pytest.mark.asyncio
async def test_public_discovery_result_controls_terminal_and_adds_sources():
    class DiscoveryAdapter(Adapter):
        def __init__(self):
            self.last_discovery_result = {"terminal": True, "discovered_sources": [{"family": "query", "query": "new"}]}
        async def open_source(self, page, spec, cursor):
            pass
        async def collect_card_refs(self, page):
            return [{"external_id": "1", "url": "https://hirehi.ru/1"}]
    search = HireHiAdaptiveSearch(DiscoveryAdapter())
    await search.add_source(SearchSource(family="query", query="old"))
    await search.collect_card_refs(object())
    assert any(s.spec.get("query") == "old" and s.exhausted for s in search.scheduler.sources.values())
    assert any(s.spec.get("query") == "new" for s in search.scheduler.sources.values())


@pytest.mark.asyncio
async def test_repeated_pages_are_bounded_and_reset_after_fresh_page():
    class RepeatAdapter(Adapter):
        def __init__(self):
            self.repeated = True
        async def open_source(self, page, spec, cursor):
            pass
        async def collect_card_refs(self, page):
            return [{"external_id": "same", "url": "https://hirehi.ru/same"}]
        @property
        def last_discovery_result(self):
            return {"repeated": self.repeated}
    adapter = RepeatAdapter()
    search = HireHiAdaptiveSearch(adapter)
    await search.add_source(SearchSource(family="query", query="repeat"))
    source = next(iter(search.scheduler.sources.values()))
    for _ in range(3):
        await search.collect_card_refs(object(), source)
    assert source.consecutive_repeats == 3 and source.exhausted
    adapter.repeated = False
    source.exhausted = False
    await search.collect_card_refs(object(), source)
    assert source.consecutive_repeats == 0


@pytest.mark.asyncio
async def test_visible_footer_sources_do_not_evict_healthy_planned_control_arms():
    class VisibleAdapter(Adapter):
        def build_source(self, spec):
            if spec.get("url"):
                return {**spec, "source_id": spec["url"]}
            return {**spec, "source_id": spec.get("query", spec.get("family"))}
    search = HireHiAdaptiveSearch(VisibleAdapter())
    for index in range(12):
        await search.add_source(SearchSource(family="query", query=f"planned-{index}", cluster="control" if index == 0 else "planned"))
    healthy = set(search.scheduler.sources)
    for index in range(50):
        await search.add_source({"url": f"https://hirehi.ru/footer/{index}", "kind": "category", "cluster": "footer"}, origin="visible")
    assert healthy <= set(search.scheduler.sources)
    assert set(search.portfolio) == set(search.scheduler.sources)


@pytest.mark.asyncio
async def test_expansion_is_bounded_and_low_novelty_has_cooldown():
    class Gateway:
        async def structured(self, role, payload, schema):
            return schema.model_validate({"sources": [{"family": "query", "query": "new-expansion"}]})
    search = HireHiAdaptiveSearch(Adapter(), Gateway())
    await search.add_source(SearchSource(family="query", query="old"))
    source = next(iter(search.scheduler.sources.values()))
    source.raw_discovered, source.unique_discovered, source.novelty = 30, 0, 0.03
    await search.collect_more(object())
    first = search.next_expansion_at
    await search.collect_more(object())
    assert search.next_expansion_at == first


@pytest.mark.asyncio
async def test_only_control_coverage_are_protected_and_expansion_skips_existing_candidates():
    class SourceAdapter(Adapter):
        def build_source(self, spec):
            return {**spec, "source_id": spec.get("query") or spec.get("category")}

    class Gateway:
        async def structured(self, role, payload, schema):
            assert payload["known_sources"] and isinstance(payload["known_sources"][0], dict)
            return schema.model_validate({"sources": [
                {"family": "category", "category": "cat-0"},
                {"family": "query", "query": "genuinely-new"},
                {"family": "query", "query": "second-new"},
            ]})
    search = HireHiAdaptiveSearch(SourceAdapter(), Gateway())
    for index in range(12):
        await search.add_source(SearchSource(family="category", category=f"cat-{index}", cluster="planned"))
    assert not search.scheduler.sources[next(iter(search.scheduler.sources))].protected
    weak = next(source for source in search.scheduler.sources.values() if source.key != "cat-0")
    weak.analyzed, weak.relevant = 25, 0
    search.analysis_count = 50
    added = await search.expand()
    assert len(added) <= 2
    assert any(source.spec.get("query") == "genuinely-new" for source in added)
