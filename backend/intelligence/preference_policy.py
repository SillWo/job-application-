from __future__ import annotations

from typing import Any

from backend.schemas.domain import DesiredJobPolicy, PreferenceFlag

from .gateway import ModelGateway


def _dump(value: Any) -> Any:
    return value.model_dump(mode="json") if hasattr(value, "model_dump") else value


def _normalize(policy: DesiredJobPolicy) -> DesiredJobPolicy:
    greens: list[PreferenceFlag] = []
    reds: list[PreferenceFlag] = []
    seen: set[tuple[str, str]] = set()
    for target, values in ((greens, policy.green_flags), (reds, policy.red_flags)):
        for flag in values:
            text = " ".join(flag.text.split()).strip()
            key = ("g" if target is greens else "r", text.casefold())
            if text and key not in seen:
                seen.add(key)
                target.append(flag.model_copy(update={"text": text}))
    for index, flag in enumerate(greens, 1):
        greens[index - 1] = flag.model_copy(update={"id": f"green-{index}"})
    for index, flag in enumerate(reds, 1):
        reds[index - 1] = flag.model_copy(update={"id": f"red-{index}"})
    salary = policy.desired_salary
    if salary and not any(f.category == "desired_salary" for f in greens):
        greens.append(PreferenceFlag(id=f"green-{len(greens)+1}", text=f"Желаемый доход от {salary.minimum_monthly_amount} {salary.currency}", category="desired_salary"))
    return DesiredJobPolicy(green_flags=greens, red_flags=reds, desired_salary=salary)


async def compile_preference_policy(gateway: ModelGateway, description: str | None) -> DesiredJobPolicy:
    text = (description or "").strip()
    if not text:
        return DesiredJobPolicy()
    result = await gateway.structured("preference_compiler", {"description": text[:2000]}, DesiredJobPolicy)
    # Salary is accepted only when the user's source text explicitly mentions
    # income expectations; never trust a hallucinated compiler field.
    salary_markers = ("зарплат", "доход", "оплат", "руб", "₽", "salary", "income")
    has_salary = any(marker in text.casefold() for marker in salary_markers)
    if not has_salary:
        result = result.model_copy(update={
            "desired_salary": None,
            "green_flags": [flag for flag in result.green_flags if flag.category != "desired_salary"],
        })
    return _normalize(result)
