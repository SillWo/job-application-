from __future__ import annotations

from typing import Any

from backend.intelligence.gateway import ModelGateway, ModelPermanentError
from backend.schemas.domain import DesiredJobPolicy, PreferenceFlag

POLICY_CONTRACT_VERSION = 2


def _dump(value: Any) -> Any:
    return value.model_dump(mode="json") if hasattr(value, "model_dump") else value


def _required_not_applicable(flag: PreferenceFlag) -> bool:
    return flag.category == "desired_salary"


def _normalize(policy: DesiredJobPolicy) -> DesiredJobPolicy:
    greens: list[PreferenceFlag] = []
    reds: list[PreferenceFlag] = []
    seen: dict[tuple[str, str], int] = {}
    for target, values in ((greens, policy.green_flags), (reds, policy.red_flags)):
        for flag in values:
            text = " ".join(flag.text.split()).strip()
            key = ("g" if target is greens else "r", text.casefold())
            if not text:
                continue
            required = bool(
                target is greens
                and flag.required
                and not _required_not_applicable(flag)
            )
            source_quote = " ".join((flag.source_quote or "").split()) or None
            if key in seen:
                index = seen[key]
                previous = target[index]
                merged_required = bool(
                    target is greens
                    and not _required_not_applicable(previous)
                    and (previous.required or required)
                )
                target[index] = previous.model_copy(
                    update={
                        "required": merged_required,
                        "source_quote": previous.source_quote or source_quote,
                    }
                )
                continue
            seen[key] = len(target)
            target.append(
                flag.model_copy(
                    update={
                        "text": text,
                        "required": required,
                        "source_quote": source_quote,
                    }
                )
            )
    for index, flag in enumerate(greens, 1):
        greens[index - 1] = flag.model_copy(update={"id": f"green-{index}"})
    for index, flag in enumerate(reds, 1):
        reds[index - 1] = flag.model_copy(update={"id": f"red-{index}"})
    salary = policy.desired_salary
    if salary and not any(f.category == "desired_salary" for f in greens):
        greens.append(PreferenceFlag(id=f"green-{len(greens)+1}", text=f"Желаемый доход от {salary.minimum_monthly_amount} {salary.currency}", category="desired_salary", required=False))
    return DesiredJobPolicy(
        contract_version=POLICY_CONTRACT_VERSION,
        green_flags=greens,
        red_flags=reds,
        desired_salary=salary,
    )


async def compile_preference_policy(gateway: ModelGateway, description: str | None) -> DesiredJobPolicy:
    text = (description or "").strip()
    if not text:
        return DesiredJobPolicy(contract_version=POLICY_CONTRACT_VERSION)
    result = await gateway.structured("preference_compiler", {"description": text[:2000]}, DesiredJobPolicy)
    source = " ".join(text.split()).casefold()
    for flag in result.green_flags:
        if not flag.required or _required_not_applicable(flag):
            continue
        quote = " ".join((flag.source_quote or "").split()).casefold()
        if not quote or quote not in source:
            raise ModelPermanentError(
                "Не удалось подтвердить основание обязательного требования в описании желаемой работы",
                error_code="policy_grounding_invalid",
            )
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
