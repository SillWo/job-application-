from typing import Literal

from pydantic import BaseModel, Field

from .gateway import ModelUnavailable
from .security import PromptInjectionDetected, assert_safe_output, sanitize_untrusted_input

CATEGORIES = ("все вакансии", "дизайн", "разработка", "DevOps", "менеджмент", "тестирование", "аналитика", "маркетинг", "продажи", "финансы", "рекрутинг")

HireHiCategory = Literal["все вакансии", "дизайн", "разработка", "DevOps", "менеджмент", "тестирование", "аналитика", "маркетинг", "продажи", "финансы", "рекрутинг"]


class HireHiCategoryChoice(BaseModel):
    category: HireHiCategory = Field(description="Exactly one allowed HireHi category")
    reason: str = ""


class JobSummary(BaseModel):
    summary: str

def deterministic_category(resume: dict) -> HireHiCategoryChoice:
    text = str(resume).casefold()
    if any(x in text for x in ("product owner", "product manager", "project manager", "менедж")):
        return HireHiCategoryChoice(category="менеджмент", reason="Совпадение с управленческим названием резюме")
    return HireHiCategoryChoice(category="все вакансии", reason="Без однозначного совпадения")

async def choose_hirehi_category(gateway, resume: dict, preference_policy=None) -> HireHiCategoryChoice:
    safe_resume = sanitize_untrusted_input(resume, context="candidate resume")
    safe_preferences = (
        sanitize_untrusted_input(preference_policy, context="candidate preferences")
        if preference_policy is not None else None
    )
    try:
        payload = {"resume": safe_resume, "allowed_categories": list(CATEGORIES)}
        if safe_preferences:
            payload["preference_policy"] = safe_preferences.model_dump(mode="json") if hasattr(safe_preferences, "model_dump") else safe_preferences
        choice = await gateway.structured("hirehi_category", payload, HireHiCategoryChoice)
        assert_safe_output(choice.model_dump(mode="json"), context="HireHi category choice")
        if choice.category in CATEGORIES:
            return choice
    except PromptInjectionDetected:
        raise
    except ModelUnavailable:
        raise
    except Exception:
        pass
    return deterministic_category(safe_resume)
