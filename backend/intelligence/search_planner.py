from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field

from .gateway import ModelUnavailable


class SearchQuery(BaseModel):
    model_config = {"extra": "forbid"}
    query: str = Field(min_length=1, max_length=120)
    relation_to_resume: str
    is_title_equivalent: bool


class SearchQueryPlan(BaseModel):
    model_config = {"extra": "forbid"}
    queries: list[SearchQuery] = Field(default_factory=list)


def _title(resume: dict[str, Any]) -> str:
    for key in ("desired_title", "title", "position"):
        value = resume.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    for key in ("general", "general_info", "common"):
        if isinstance(resume.get(key), dict):
            found = _title(resume[key])
            if found:
                return found
    return ""


def _words(value: str) -> set[str]:
    return {word for word in re.findall(r"[\w-]+", value.casefold()) if len(word) > 2}


def _fallback(resumes: list[dict[str, Any]], limit: int) -> list[str]:
    values: list[str] = []
    for resume in resumes:
        for key in ("adjacent_titles", "related_titles", "keywords"):
            raw = resume.get(key, [])
            raw = raw.split(",") if isinstance(raw, str) else raw
            if isinstance(raw, list):
                # Legacy strings cannot be classified as translations or core-function
                # synonyms safely. Only explicitly classified candidates are trusted.
                values.extend(
                    str(item.get("query", "")).strip()
                    for item in raw
                    if (isinstance(item, dict)
                        and item.get("is_title_equivalent") is False
                        and isinstance(item.get("relation_to_resume"), str)
                        and item.get("relation_to_resume", "").strip())
                )
    return _sanitize(values, resumes, limit)


def _sanitize(values: list[str], resumes: list[dict[str, Any]], limit: int) -> list[str]:
    originals = [_title(item) for item in resumes]
    original_words = [_words(item) for item in originals if item]
    result: list[str] = []
    seen: set[str] = set()
    for raw in values:
        value = " ".join(str(raw).split()).strip(" ,;|\"'")
        if not value or len(value) > 120:
            continue
        key = value.casefold()
        words = _words(value)
        if key in seen or any(words == source or (words and source and (words <= source or source <= words)) for source in original_words):
            continue
        seen.add(key)
        result.append(value[:120])
        if len(result) >= limit:
            break
    return result


async def plan_search_queries(gateway, resumes: list[dict[str, Any]], limit: int = 12, preference_policy: Any = None) -> list[str]:
    limit = max(0, min(limit, 12))
    if not resumes or not limit:
        return []
    payload = {"resumes": resumes, "limit": limit}
    if preference_policy:
        payload["preference_policy"] = preference_policy.model_dump(mode="json") if hasattr(preference_policy, "model_dump") else preference_policy
    try:
        planned = await gateway.structured("search_planner", payload, SearchQueryPlan)
        values = [item.query for item in planned.queries if not item.is_title_equivalent]
    except (ModelUnavailable, ValueError, TypeError, AttributeError):
        values = []
    result = _sanitize(values, resumes, limit)
    if result:
        return result
    return _fallback(resumes, limit)
