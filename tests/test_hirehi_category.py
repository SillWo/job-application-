import pytest

from backend.intelligence.gateway import ModelGateway, _system_prompt_for_role
from backend.intelligence.hirehi_category import (
    CATEGORIES,
    HireHiCategoryChoice,
    choose_hirehi_category,
    deterministic_category,
)


def test_product_owner_maps_to_management():
    assert deterministic_category({"desired_title": "Product Owner"}).category == "менеджмент"

def test_unknown_falls_back_to_all():
    assert deterministic_category({"desired_title": "Astronaut"}).category == "все вакансии"


@pytest.mark.asyncio
async def test_model_invalid_category_falls_back():
    class Gateway:
        async def structured(self, *args): return HireHiCategoryChoice(category="unknown")
    choice = await choose_hirehi_category(Gateway(), {"desired_title": "Product Owner"})
    assert choice.category in CATEGORIES

@pytest.mark.asyncio
async def test_mock_gateway_category_and_registered_prompt():
    gateway = ModelGateway(provider="mock")
    choice = await gateway.structured("hirehi_category", {"resume": {"desired_title": "Product Owner"}, "allowed_categories": list(CATEGORIES)}, HireHiCategoryChoice)
    assert choice.category == "менеджмент"
    assert "allowed_categories" in _system_prompt_for_role("hirehi_category", {"allowed_categories": list(CATEGORIES)})
