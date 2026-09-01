from typing import Literal

from pydantic import BaseModel, Field

from .gateway import ModelUnavailable

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
    try:
        payload = {"resume": resume, "allowed_categories": list(CATEGORIES)}
        if preference_policy:
            payload["preference_policy"] = preference_policy.model_dump(mode="json") if hasattr(preference_policy, "model_dump") else preference_policy
        choice = await gateway.structured("hirehi_category", payload, HireHiCategoryChoice)
        if choice.category in CATEGORIES:
            return choice
    except ModelUnavailable:
        raise
    except Exception:
        pass
    return deterministic_category(resume)
