import pytest

from backend.intelligence.gateway import ModelUnavailable
from backend.intelligence.search_planner import SearchQueryPlan, plan_search_queries

RESUME = {
    "desired_title": "Менеджер проекта",
    "skills": ["аналитика процессов", "SQL"],
    "adjacent_titles": ["Операционный координатор", "Менеджер проекта"],
    "related_titles": ["Project Manager", "Руководитель проектов"],
}


def test_search_query_requires_classification_fields():
    with pytest.raises(ValueError):
        SearchQueryPlan.model_validate({"queries": [{"query": "Adjacent role"}]})


class Gateway:
    def __init__(self, result=None, error=None):
        self.result, self.error = result, error
        self.roles = []

    async def structured(self, role, payload, schema):
        self.roles.append(role)
        if self.error:
            raise self.error
        return self.result


@pytest.mark.asyncio
async def test_model_path_filters_exact_substring_and_equivalent():
    gateway = Gateway(SearchQueryPlan.model_validate({"queries": [
        {"query": "Менеджер проекта", "relation_to_resume": "same", "is_title_equivalent": True},
        {"query": "Руководитель проектов", "relation_to_resume": "same", "is_title_equivalent": True},
        {"query": "Project Manager", "relation_to_resume": "same", "is_title_equivalent": True},
        {"query": "Операционный координатор", "relation_to_resume": "adjacent", "is_title_equivalent": False},
    ]}))
    assert await plan_search_queries(gateway, [RESUME]) == ["Операционный координатор"]
    assert gateway.roles == ["search_planner"]


@pytest.mark.asyncio
async def test_model_failure_uses_explicit_safe_fallback_and_limit():
    resume = {**RESUME, "adjacent_titles": ["A", "B"], "related_titles": [
        "Project Manager", "Руководитель проектов"
    ]}
    assert await plan_search_queries(Gateway(error=ModelUnavailable("down")), [resume], 2) == []


@pytest.mark.asyncio
async def test_structured_fallback_candidate_is_trusted_only_when_classified():
    resume = {"desired_title": "Core role", "keywords": [
        {"query": "Adjacent role", "relation_to_resume": "shared workflow", "is_title_equivalent": False},
        {"query": "Core role", "relation_to_resume": "same", "is_title_equivalent": True},
        "Legacy role",
    ]}
    assert await plan_search_queries(Gateway(error=ModelUnavailable("down")), [resume]) == ["Adjacent role"]


@pytest.mark.asyncio
async def test_empty_input_is_safe():
    assert await plan_search_queries(Gateway(), []) == []
