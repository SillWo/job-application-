"""Planning primitives for the independent HireHi adaptive search engine.

The planner only suggests structured queries/categories.  URL construction and
allow-list validation always remain the responsibility of the adapter.
"""
from __future__ import annotations

import re
from hashlib import sha256
from math import isfinite
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from backend.intelligence.security import (
    PromptInjectionDetected,
    assert_safe_output,
    sanitize_untrusted_input,
)
from backend.schemas.domain import DesiredJobPolicy


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _items(values: Any) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        values = re.split(r"[,;\n]", values)
    result, seen = [], set()
    for value in values or []:
        item = _clean(value)
        if item and item.casefold() not in seen:
            result.append(item)
            seen.add(item.casefold())
    return result


def _unwrap(value: Any) -> Any:
    """Read SourceField values while retaining support for legacy scalars."""
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    if isinstance(value, dict) and "value" in value:
        return value.get("value")
    return value


def _path(value: Any, *names: str) -> Any:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    if not isinstance(value, dict):
        return None
    for name in names:
        if name in value:
            return _unwrap(value[name])
    return None


class SearchProfile(BaseModel):
    """Immutable, normalized criteria used by every HireHi source."""

    model_config = ConfigDict(extra="forbid")
    target_roles: list[str] = Field(default_factory=list)
    adjacent_roles: list[str] = Field(default_factory=list)
    excluded_roles: list[str] = Field(default_factory=list)
    required_skills: list[str] = Field(default_factory=list)
    preferred_skills: list[str] = Field(default_factory=list)
    excluded_skills: list[str] = Field(default_factory=list)
    grades: list[str] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)
    remote_policy: str | None = None
    languages: list[str] = Field(default_factory=list)
    salary_floor: float | int | None = Field(default=None, ge=0)
    application_types: list[str] = Field(default_factory=list)
    hard_constraints: list[str] = Field(default_factory=list)
    soft_preferences: list[str] = Field(default_factory=list)

    @field_validator(
        "target_roles", "adjacent_roles", "excluded_roles", "required_skills", "preferred_skills",
        "excluded_skills", "grades", "locations", "languages", "application_types",
        "hard_constraints", "soft_preferences", mode="before",
    )
    @classmethod
    def normalize_lists(cls, value: Any) -> list[str]:
        return _items(value)

    @field_validator("remote_policy", mode="before")
    @classmethod
    def normalize_policy(cls, value: Any) -> str | None:
        value = _clean(value).casefold() if value is not None else ""
        return value or None

    @field_validator("salary_floor", mode="before")
    @classmethod
    def normalize_salary(cls, value: Any) -> Any:
        if value is None or isinstance(value, bool):
            return None
        try:
            numeric = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        return value if isfinite(numeric) and 0 <= numeric <= 1_000_000_000 else None


class HireHiSourceFilters(BaseModel):
    """Allow-listed visible HireHi query parameters; no arbitrary keys/URLs."""

    model_config = ConfigDict(extra="forbid")
    level: list[Literal["intern", "junior", "middle", "senior", "lead", "head"]] = Field(default_factory=list)
    format: list[Literal["удалённо", "офис", "гибрид", "удалённо по РФ"]] = Field(default_factory=list)
    region: list[Literal["Europe & UK", "CIS", "Russia"]] = Field(default_factory=list)
    english: Literal["english", "no_english"] | None = None
    direct_contact: list[Literal["direct_contact", "linkedin", "email", "telegram", "exclude"]] = Field(default_factory=list)
    salary_from: int | None = Field(default=None, ge=0, le=1_000_000_000)
    salary_to: int | None = Field(default=None, ge=0, le=1_000_000_000)

    @field_validator("level", "format", "direct_contact", "region", mode="before")
    @classmethod
    def normalize_filter_lists(cls, value: Any) -> list[str]:
        return _items(value)

    @field_validator("format", mode="before")
    @classmethod
    def normalize_formats(cls, value: Any) -> list[str]:
        aliases = {"remote": "удалённо", "office": "офис", "в офисе": "офис", "hybrid": "гибрид",
                   "удалённо по рф": "удалённо по РФ"}
        return [aliases.get(item.casefold(), item) for item in _items(value)]


class SearchSource(BaseModel):
    """A safe source suggestion; intentionally has no URL field."""

    model_config = ConfigDict(extra="forbid")
    source_id: str | None = Field(default=None, max_length=100)
    family: Literal["specialization", "category", "query", "coverage", "pro_recommendations"]
    query: str | None = Field(default=None, max_length=160)
    field: Literal["name", "description"] = "name"
    category: str | None = Field(default=None, max_length=100)
    cluster: str = Field(default="", max_length=100)
    rationale: str = Field(default="", max_length=300)
    pro_only: bool = False
    filters: HireHiSourceFilters = Field(default_factory=HireHiSourceFilters)

    @field_validator("query", "category", "cluster", "rationale", mode="before")
    @classmethod
    def normalize_text(cls, value: Any) -> str:
        return _clean(value)

    @model_validator(mode="after")
    def require_payload(self) -> SearchSource:
        if self.family == "query" and not self.query:
            raise ValueError("query source requires query")
        if self.family in {"category", "specialization"} and not (self.category or self.query):
            raise ValueError("category source requires category")
        return self

    @property
    def key(self) -> str:
        return sha256(repr(sorted(self.model_dump(mode="json", exclude_none=True).items())).encode()).hexdigest()[:20]


class SourceStats(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_id: str
    family: str
    raw_discovered: int = Field(default=0, ge=0)
    unique_discovered: int = Field(default=0, ge=0)
    analyzed: int = Field(default=0, ge=0)
    relevant: int = Field(default=0, ge=0)
    duplicates: int = Field(default=0, ge=0)
    failures: int = Field(default=0, ge=0)
    elapsed_seconds: float = Field(default=0, ge=0)
    cursor: Any = None
    exhausted: bool = False
    cooldown_until: int | None = None
    last_used_at: int | None = None
    novelty: float = 1.0
    availability: float = 1.0
    cost: float = 0.0

    @property
    def precision(self) -> float:
        return self.relevant / self.analyzed if self.analyzed else 0.0


class HireHiPortfolio(BaseModel):
    model_config = ConfigDict(extra="forbid")
    profile: SearchProfile
    sources: list[SearchSource] = Field(default_factory=list, max_length=12)


class HireHiCheckpoint(BaseModel):
    """JSON checkpoint envelope (nested runtime state remains structured data)."""

    model_config = ConfigDict(extra="forbid")
    algorithm_version: Literal["hirehi_adaptive_v3"]
    criteria_hash: str
    profile_snapshot: SearchProfile
    portfolio: list[dict[str, Any]] = Field(default_factory=list)
    source_stats: dict[str, Any] = Field(default_factory=dict)
    source_cursors: dict[str, Any] = Field(default_factory=dict)
    pending_refs: list[dict[str, Any]] = Field(default_factory=list)
    seen_exact_ids: list[str] = Field(default_factory=list)
    semantic_clusters: dict[str, Any] = Field(default_factory=dict)
    origin_map: dict[str, list[str]] = Field(default_factory=dict)
    analyzed_ids: list[str] = Field(default_factory=list)
    relevant_ids: list[str] = Field(default_factory=list)
    audit_sample_state: dict[str, Any] = Field(default_factory=dict)
    exploration_rng_state: list[Any] = Field(default_factory=list)
    next_expansion_at: int = 50
    budgets: dict[str, Any] = Field(default_factory=dict)
    verdict_by_id: dict[str, str] = Field(default_factory=dict)
    observed_by_source: dict[str, list[str]] = Field(default_factory=dict)
    archived_sources: dict[str, Any] = Field(default_factory=dict)
    scheduler: dict[str, Any] = Field(default_factory=dict)
    search_exhausted: bool = False
    rejection_reasons: dict[str, int] = Field(default_factory=dict)
    relevant_examples: list[dict[str, Any]] = Field(default_factory=list, max_length=12)


SearchCheckpoint = HireHiCheckpoint
Checkpoint = HireHiCheckpoint


def criteria_hash(profile: SearchProfile, criteria_context: Any = None, resumes: Any = None,
                  preference_policy: Any = None) -> str:
    import json

    def jsonable(value: Any) -> Any:
        if isinstance(value, BaseModel):
            return jsonable(value.model_dump(mode="json"))
        if isinstance(value, dict):
            return {str(key): jsonable(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [jsonable(item) for item in value]
        return value

    payload = {"profile": profile.model_dump(mode="json"), "criteria_context": criteria_context or {},
               "resumes": resumes or [], "preference_policy": preference_policy or {}}
    encoded = json.dumps(jsonable(payload), sort_keys=True, ensure_ascii=False, default=str)
    return sha256(encoded.encode()).hexdigest()


def _resume_value(resume: Any, *names: str) -> Any:
    if isinstance(resume, BaseModel):
        resume = resume.model_dump(mode="json")
    if isinstance(resume, dict):
        for name in names:
            if resume.get(name):
                return resume[name]
    return None


def deterministic_profile(resumes: list[Any] | None, preference_policy: Any = None) -> SearchProfile:
    resumes = resumes or []
    target, skills, grades, locations, languages, application_types = [], [], [], [], [], []
    work_formats: list[str] = []
    for resume in resumes:
        # ResumeProfessionalView/SiteResumeSnapshot shape.  Every field is
        # availability-wrapped, so only ``value`` is trusted as content.
        target_obj = _path(resume, "target") or {}
        location_obj = _path(resume, "location") or {}
        title = _path(target_obj, "desired_title", "title")
        target.extend(_items([title] if isinstance(title, str) else title or []))
        target.extend(_items(_path(target_obj, "specializations") or []))
        grades.extend(_items([_path(target_obj, "grade")] if _path(target_obj, "grade") else []))
        locations.extend(_items([_path(location_obj, "residence")] if _path(location_obj, "residence") else []))
        formats = _items(_path(target_obj, "work_formats") or [])
        work_formats.extend(formats)
        employment = _items(_path(target_obj, "employment_types") or [])
        application_types.extend(employment)
        raw_skills = _path(resume, "skills") or []
        for skill in raw_skills if isinstance(raw_skills, list) else []:
            name = _path(skill, "name")
            skills.extend(_items([name] if isinstance(name, str) else name or []))
        raw_languages = _path(resume, "languages") or []
        for language in raw_languages if isinstance(raw_languages, list) else []:
            name = _path(language, "language")
            languages.extend(_items([name] if isinstance(name, str) else name or []))
        # Legacy flat shapes remain supported below.
        title = _resume_value(resume, "desired_title", "target_title", "title")
        if isinstance(title, str) and title:
            target.extend(_items([title]))
        flat_skills = _resume_value(resume, "skills", "skill_names")
        if isinstance(flat_skills, (list, tuple)) and all(isinstance(item, str) for item in flat_skills):
            skills.extend(_items(flat_skills))
        flat_grades = _resume_value(resume, "grades", "grade")
        if isinstance(flat_grades, (str, list, tuple)):
            grades.extend(_items(flat_grades))
        flat_locations = _resume_value(resume, "locations", "location")
        if isinstance(flat_locations, (str, list, tuple)):
            locations.extend(_items(flat_locations))
        flat_languages = _resume_value(resume, "languages", "language")
        if isinstance(flat_languages, (str, list, tuple)) and all(isinstance(item, str) for item in flat_languages if not isinstance(flat_languages, str)):
            languages.extend(_items(flat_languages))
        flat_formats = _resume_value(resume, "work_formats", "remote_policy")
        if isinstance(flat_formats, (str, list, tuple)):
            work_formats.extend(_items(flat_formats))
        flat_employment = _resume_value(resume, "employment_types", "application_types")
        if isinstance(flat_employment, (str, list, tuple)):
            application_types.extend(_items(flat_employment))
    base = SearchProfile(target_roles=target, preferred_skills=skills, grades=grades, locations=locations,
                         languages=languages, application_types=application_types,
                         remote_policy=work_formats[0] if work_formats else None)
    if preference_policy is None:
        return base
    if isinstance(preference_policy, DesiredJobPolicy):
        policy = preference_policy
        explicit = {
            "soft_preferences": [flag.text for flag in policy.green_flags],
            "hard_constraints": [flag.text for flag in policy.red_flags],
        }
        if policy.desired_salary:
            explicit["salary_floor"] = policy.desired_salary.minimum_monthly_amount
    elif isinstance(preference_policy, dict) and {"green_flags", "red_flags", "desired_salary"} & set(preference_policy):
        policy_input = dict(preference_policy)
        salary_input = policy_input.get("desired_salary")
        if isinstance(salary_input, dict) and isinstance(salary_input.get("minimum", salary_input.get("minimum_monthly_amount")), (int, float)):
            amount = salary_input.get("minimum", salary_input.get("minimum_monthly_amount"))
            if not isinstance(amount, bool) and isfinite(float(amount)) and 0 < float(amount) <= 1_000_000_000:
                policy_input["desired_salary"] = {**salary_input, "minimum_monthly_amount": int(amount)}
        policy = DesiredJobPolicy.model_validate(policy_input)
        explicit = {
            "soft_preferences": [flag.text for flag in policy.green_flags],
            "hard_constraints": [flag.text for flag in policy.red_flags],
        }
        if policy.desired_salary:
            explicit["salary_floor"] = policy.desired_salary.minimum_monthly_amount
    elif isinstance(preference_policy, BaseModel):
        explicit = preference_policy.model_dump(exclude_unset=True)
    elif isinstance(preference_policy, dict):
        explicit = dict(preference_policy)
    else:
        explicit = {}
    merged = base.model_dump()
    for key, value in explicit.items():
        if key in merged:
            merged[key] = value
    # A DesiredJobPolicy red flag is explanatory input, not proof of an
    # excluded role/skill.  Never synthesize excluded_* from it.
    return SearchProfile.model_validate(merged)


def _fallback_sources(profile: SearchProfile, pro_enabled: bool) -> list[SearchSource]:
    role = profile.target_roles[0] if profile.target_roles else ""
    format_alias = {"remote": "удалённо", "office": "офис", "в офисе": "офис", "hybrid": "гибрид",
                    "удалённо по рф": "удалённо по РФ"}
    live_format = format_alias.get((profile.remote_policy or "").casefold(), profile.remote_policy or "")
    location_text = " ".join(profile.locations).casefold()
    region = []
    if any(marker in location_text for marker in ("russia", "россия", "москва", "санкт-петербург", "екатеринбург", "казань", "новосибирск")):
        region = ["Russia"]
    elif any(marker in location_text for marker in ("cis", "снг")):
        region = ["CIS"]
    elif any(marker in location_text for marker in ("europe", "европа", "uk", "англия")):
        region = ["Europe & UK"]
    filters = HireHiSourceFilters(
        level=[profile.grades[0]] if profile.grades and profile.grades[0] in {"intern", "junior", "middle", "senior", "lead", "head"} else [],
        format=[live_format] if live_format in {"удалённо", "офис", "гибрид", "удалённо по РФ"} else [],
        region=region,
        salary_from=(int(profile.salary_floor) if isinstance(profile.salary_floor, (int, float))
                     and not isinstance(profile.salary_floor, bool) and isfinite(float(profile.salary_floor))
                     and 0 <= profile.salary_floor <= 1_000_000_000 else None),
        direct_contact=(["direct_contact"] if profile.application_types and profile.application_types[0] in {"direct", "direct_contact"}
                        else [profile.application_types[0]] if profile.application_types and profile.application_types[0] in {"linkedin", "email", "telegram", "exclude"} else []),
    )
    result: list[SearchSource] = []
    if role:
        result.extend([
            SearchSource(family="specialization", category=role, cluster="target", rationale="target role", filters=filters),
            SearchSource(family="query", query=role, field="name", cluster="title", rationale="exact title", filters=filters),
            SearchSource(family="query", query=role, field="description", cluster="description", rationale="description title", filters=filters),
        ])
    for skill in (profile.required_skills[:3] or profile.preferred_skills[:3]):
        result.append(SearchSource(family="query", query=f"{role} +{skill}".strip(), cluster="skills", rationale="role and skill", filters=filters))
    for role2 in profile.adjacent_roles[:2]:
        result.append(SearchSource(family="query", query=role2, cluster="adjacent", rationale="adjacent role", filters=filters))
    result.extend([
        SearchSource(family="category", category=role or "all", cluster="coverage", rationale="coverage", filters=filters),
        SearchSource(family="coverage", category=role or "all", cluster="control", rationale="control coverage", filters=filters),
    ])
    # A sparse resume must still start a useful, diverse portfolio.  These are
    # suggestions only; the adapter maps them to current HireHi UI routes.
    if len(result) < 8:
        for index, query in enumerate((role, "product", "project", "analyst", "manager", "operations", "qa", "devops", "marketing")):
            if query:
                result.append(SearchSource(family="query", query=query, cluster=f"fallback-{index}", rationale="coverage fallback", filters=filters))
    if pro_enabled:
        result.append(SearchSource(family="pro_recommendations", cluster="pro", rationale="personal recommendations", pro_only=True))
    unique, keys = [], set()
    for source in result:
        if source.key not in keys:
            keys.add(source.key)
            unique.append(source)
    return unique[:12]


async def plan_hirehi_portfolio(
    gateway: Any, resumes: list[Any] | None = None, preference_policy: Any = None, *,
    pro_enabled: bool = False, known_sources: list[Any] | None = None,
    rejection_reasons: dict[str, int] | None = None, relevant_examples: list[Any] | None = None,
) -> HireHiPortfolio:
    safe_resumes = sanitize_untrusted_input(resumes or [], context="HireHi resumes")
    safe_preferences = sanitize_untrusted_input(preference_policy, context="HireHi preferences") if preference_policy is not None else None
    profile = deterministic_profile(safe_resumes, safe_preferences)
    payload = {
        "resumes": safe_resumes, "profile": profile.model_dump(mode="json"),
        "known_sources": sanitize_untrusted_input(known_sources or [], context="HireHi known sources"),
        "rejection_reasons": sanitize_untrusted_input(rejection_reasons or {}, context="HireHi rejection reasons"),
        "relevant_examples": sanitize_untrusted_input(relevant_examples or [], context="HireHi relevant examples"),
        "pro_enabled": bool(pro_enabled), "max_sources": 12,
    }
    proposed: list[SearchSource] = []
    if gateway is not None:
        try:
            class _Plan(BaseModel):
                model_config = ConfigDict(extra="forbid")
                sources: list[SearchSource] = Field(default_factory=list, max_length=12)

            result = await gateway.structured("hirehi_adaptive_planner", payload, _Plan)
            assert_safe_output(result.model_dump(mode="json"), context="HireHi adaptive plan")
            proposed = [s for s in result.sources if (pro_enabled or not s.pro_only) and s.family != "related"]
        except (PromptInjectionDetected, ValueError, KeyError, LookupError, RuntimeError):
            proposed = []
    all_sources, seen = [], set()
    for source in [*proposed, *_fallback_sources(profile, pro_enabled)]:
        if source.key not in seen:
            seen.add(source.key)
            all_sources.append(source)
    return HireHiPortfolio(profile=profile, sources=all_sources[:12])


async def plan_portfolio(gateway: Any, resumes: list[Any] | None, preference_policy: Any = None, **kwargs: Any) -> list[dict[str, Any]]:
    portfolio = await plan_hirehi_portfolio(gateway, resumes, preference_policy, **kwargs)
    return [source.model_dump(mode="json", exclude_none=True) for source in portfolio.sources]


build_search_profile = deterministic_profile


class HireHiPlanner:
    """Small object facade for integrations that keep planner dependencies."""

    def __init__(self, gateway: Any):
        self.gateway = gateway

    def build_profile(self, resumes: list[Any] | None, preference_policy: Any = None) -> SearchProfile:
        return deterministic_profile(resumes, preference_policy)

    async def plan(self, resumes: list[Any] | None, preference_policy: Any = None, **kwargs: Any) -> HireHiPortfolio:
        return await plan_hirehi_portfolio(self.gateway, resumes, preference_policy, **kwargs)


__all__ = ["SearchProfile", "HireHiSourceFilters", "SearchSource", "SourceStats", "HireHiPortfolio", "HireHiCheckpoint", "SearchCheckpoint", "Checkpoint", "criteria_hash", "deterministic_profile", "build_search_profile", "HireHiPlanner", "plan_hirehi_portfolio", "plan_portfolio"]
