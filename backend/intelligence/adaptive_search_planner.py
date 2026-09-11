"""Bounded expansion rounds; no lifetime query cap and no title exclusion."""
from typing import Literal

from pydantic import BaseModel, Field

from backend.intelligence.search_planner import _title
from backend.intelligence.security import (
    PromptInjectionDetected,
    assert_safe_output,
    sanitize_untrusted_input,
)


class PortfolioQuery(BaseModel):
    model_config = {"extra": "forbid"}
    query: str = Field(min_length=1, max_length=120)
    field: Literal["name", "description"] = "name"
    cluster: str = Field(min_length=1, max_length=120)
    evidence: str = Field(min_length=1, max_length=300)


class PortfolioPlan(BaseModel):
    model_config = {"extra": "forbid"}
    queries: list[PortfolioQuery] = Field(default_factory=list, max_length=12)


async def plan_portfolio(gateway, resumes, preference_policy=None, *, known=None, relevant=None):
    safe_resumes = sanitize_untrusted_input(resumes, context="candidate resumes")
    safe_known = sanitize_untrusted_input(known or [], context="known search queries")
    safe_relevant = sanitize_untrusted_input(relevant or [], context="relevant search examples")
    safe_preferences = (
        sanitize_untrusted_input(preference_policy, context="candidate search preferences")
        if preference_policy is not None else None
    )
    payload = {"resumes": safe_resumes, "known_queries": safe_known,
               "relevant_examples": safe_relevant, "limit": 12}
    if safe_preferences:
        payload["preference_policy"] = safe_preferences.model_dump(mode="json")
    try:
        plan = await gateway.structured("adaptive_search_planner", payload, PortfolioPlan)
    except PromptInjectionDetected:
        plan = PortfolioPlan(queries=[])
    assert_safe_output(plan.model_dump(mode="json"), context="adaptive search plan")
    # The core title is a deterministic seed even when the model omits it.
    seeds = [PortfolioQuery(query=_title(r)[:120], cluster=_title(r)[:120], evidence="Selected resume title")
             for r in safe_resumes if _title(r)] if not safe_known else []
    result, seen = [], set()
    for entry in [*seeds, *plan.queries]:
        query = " ".join(entry.query.split())
        key = (query.casefold(), entry.field)
        if query and key not in seen:
            seen.add(key)
            result.append(entry.model_copy(update={"query": query}).model_dump())
    assert_safe_output(result, context="adaptive search queries")
    return result
