"""Bounded expansion rounds; no lifetime query cap and no title exclusion."""
from typing import Literal

from pydantic import BaseModel, Field

from backend.intelligence.search_planner import _title


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
    payload = {"resumes": resumes, "known_queries": known or [],
               "relevant_examples": relevant or [], "limit": 12}
    if preference_policy:
        payload["preference_policy"] = preference_policy.model_dump(mode="json")
    plan = await gateway.structured("adaptive_search_planner", payload, PortfolioPlan)
    # The core title is a deterministic seed even when the model omits it.
    seeds = [PortfolioQuery(query=_title(r)[:120], cluster=_title(r)[:120], evidence="Selected resume title")
             for r in resumes if _title(r)] if not known else []
    result, seen = [], set()
    for entry in [*seeds, *plan.queries]:
        query = " ".join(entry.query.split())
        key = (query.casefold(), entry.field)
        if query and key not in seen:
            seen.add(key)
            result.append(entry.model_copy(update={"query": query}).model_dump())
    return result
