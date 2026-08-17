from __future__ import annotations

import re

from backend.schemas.domain import SearchPolicy

_NON_FACT_FLAG_TERMS = re.compile(
    r"(зарплат|salary|оклад|ожидан|отклик|reply|application|cover\s+letter|сопровод|письм|заполн|форма|вопрос|ответ|анкета|отправ|"
    r"удал[её]н\w*\s+работ|remote|гибрид\w*\s+работ|hybrid|офис\w*\s+работ|office|вахт\w*|"
    r"удал[её]н\w*|гибрид\w*|очно|разъезд\w*\s+работ|mobile|rotational|"
    r"красноярск\w*|москв\w*|санкт-петербург\w*|росси\w*|любом\s+город\w*)",
    re.I,
)

_POSITIVE_FILLER_FRAGMENT = re.compile(
    r"^(?:а\s+так\s+же\s+иные\s+вакансии|так\s+же\s+иные\s+вакансии|"
    r"даже\b|не\s+смотря\b|в\s+котор(?:ых|ые)\b|котор(?:ых|ые)\b)$",
    re.I,
)


def _normalize_positive_flag(flag: str) -> str:
    """Normalize a copied subordinate clause into a stable vacancy fact."""
    flag = re.sub(
        r"^(Вакансия\s+предлагает)\s+работа\s+с\b",
        r"\1 работу с",
        flag,
        flags=re.I,
    )
    match = re.match(
        r"^(Вакансия\s+предлагает)\s+в\s+котор(?:ых|ые)\s+могут\s+быть\s+применены\s+навыки\s+(.+)$",
        flag,
        re.I,
    )
    if match:
        return f"{match.group(1)} навыки {match.group(2).strip()}"
    return flag


def _sanitize_flags(flags: list[str], *, positive: bool = False) -> list[str]:
    """Keep only atomic vacancy predicates returned by the compiler role."""
    result = []
    for raw in flags:
        flag = str(raw).strip()
        if positive:
            flag = _normalize_positive_flag(flag)
        if (
            not flag.lower().startswith("вакансия ")
            or _NON_FACT_FLAG_TERMS.search(flag)
        ):
            continue
        if flag not in result:
            result.append(flag)
    return result


def _meaningful_positive_fragment(fragment: str) -> bool:
    """Reject only deterministic filler fragments, never model flags."""
    value = re.sub(r"\s+", " ", fragment.strip(" ,")).lower()
    if not value or value in {
        "а", "и", "или", "так же", "целевой аудитории", "в", "на", "с", "по", "из", "для", "к", "о", "не",
    }:
        return False
    return _POSITIVE_FILLER_FRAGMENT.match(value) is None


def _request_fact_flags(request_text: str) -> tuple[list[str], list[str]]:
    """Recover explicit vacancy facts the model may omit during compilation.

    This is deliberately a coverage pass, not a replacement NLP compiler: it
    only handles clear user predicates and relies on the same sanitizer as the
    model output. In particular, response instructions, salary and work format
    remain excluded.
    """
    text = re.sub(r"\s+", " ", request_text.strip())
    green: list[str] = []
    red: list[str] = []

    # Negative response instructions often hide the actual vacancy predicate
    # in a conditional clause ("не откликайся ... если нужно работать с X").
    for match in re.finditer(
        r"\bне\s+(?:откликайся|рассматривай|подавай|отправляй)\b.*?\b(?:если|когда|где)\b\s+([^.;!?]+)",
        text,
        re.I,
    ):
        clause = re.sub(r"^в\s+вакансии\s+", "", match.group(1).strip(), flags=re.I)
        clause = re.sub(r"^(?:нужно|требуется|придется|приходится)\s+", "", clause, flags=re.I)
        clause = re.sub(r"\s*,\s*как\s*", " как ", clause, flags=re.I)
        # Split independently stated alternatives/list items. Keep commas in
        # constructions such as "услугами, как продуктом" together.
        parts = re.split(r"\s+(?:или|и)\s+|;\s*|,(?!\s*как\b)", clause, flags=re.I)
        for part in parts:
            part = part.strip(" ,")
            if part:
                red.append(f"Вакансия предполагает {part}")

    # Cover common explicit negative preference forms not guaranteed to be
    # phrased with "без".
    for match in re.finditer(
        r"\bне\s+интерес(?:н\w*|у\w*)\s+(?:вакансии\s+)?(?:с|по|в)?\s*([^.;!?]+)", text, re.I
    ):
        clause = re.sub(r"^(?:вакансии\s+)?(?:по|с|в)\s+", "", match.group(1).strip(), flags=re.I)
        for part in re.split(r"\s+(?:или|и)\s+|,(?!\s*как\b)", clause, flags=re.I):
            part = part.strip(" ,")
            if part:
                red.append(f"Вакансия связана с {part}")

    # Positive role/domain preferences are useful coverage when the model
    # returns only negative flags. Stop before a separate negative clause.
    for match in re.finditer(
        r"(?<!не )\b(?:ищу|интересуют|интересны|предпочитаю|подходят)\s+(?:вакансии\s+)?([^.;!?]+)", text, re.I
    ):
        clause = re.split(r"\s+(?:без|не)\s+", match.group(1), maxsplit=1, flags=re.I)[0].strip(" ,")
        trigger = match.group(0).split(maxsplit=1)[0].lower()
        if trigger == "подходят":
            for level in ("junior", "middle"):
                if re.search(rf"\b{level}\b", clause, re.I):
                    green.append(f"Вакансия рассчитана на {level}")
            if re.search(r"стажировк", clause, re.I):
                green.append("Вакансия предлагает стажировку")
            continue
        parts = re.split(r"\s+(?:или|и)\s+|,(?!\s*как\b)|/", clause, flags=re.I)
        for part in parts:
            part = part.strip(" ,")
            if part.lower() == "целевой аудитории":
                green.append("Вакансия требует навыки анализа целевой аудитории")
                continue
            if _meaningful_positive_fragment(part):
                green.append(f"Вакансия предлагает {part}")
    return _sanitize_flags(green, positive=True), _sanitize_flags(red)


def _merge_request_coverage(
    request_text: str, green_flags: list[str], red_flags: list[str]
) -> tuple[list[str], list[str]]:
    requested_green, requested_red = _request_fact_flags(request_text)

    def dedup_terms(value: str) -> set[str]:
        """Semantic subject terms used only for duplicate elimination."""
        ignored = {
            "вакансия", "связана", "предлагает", "предполагает", "требует",
            "включает", "относится", "является", "рассчитана", "на", "с", "в",
            "к", "по", "для", "уровень", "позиция", "работа", "работать",
            "указан", "требуемый", "сфера", "область", "направление", "ней",
        }
        return {
            word.lower() if len(word) <= 4 else word.lower()[:4]
            for word in re.findall(r"[\w-]+", value)
            if len(word) >= 3
            and word.lower() not in ignored
            and not any(
                word.lower().startswith(prefix)
                for prefix in (
                    "ваканс", "связан", "предлаг", "предпол", "треб", "включ",
                    "относ", "явля", "рассчит", "позиц", "уров", "работ",
                    "указан", "сфер", "област", "направлен",
                )
            )
        }

    def terms(value: str) -> set[str]:
        ignored = {
            "вакансия", "связана", "предлагает", "предполагает", "продукт", "product",
            "роль", "работа", "работать", "опыт", "лет", "более", "старше", "manager", "management", "менеджер",
            "пози", "рассчит", "треб", "связ", "услов", "предлаг", "включ", "должн", "нужн",
        }
        return {
            word.lower() if len(word) <= 4 else word.lower()[:5]
            for word in re.findall(r"[\w-]+", value)
            if len(word) >= 3
            and word.lower() not in ignored
            and not any(
                word.lower().startswith(prefix)
                for prefix in (
                    "продукт", "product", "менедж", "manager", "пози", "рассчит", "треб",
                    "связ", "услов", "предлаг", "включ", "должн", "нужн", "сфер", "област", "направлен",
                )
            )
        }

    def merge(base: list[str], additions: list[str], *, allow_subset: bool) -> list[str]:
        # Sanitize model output before deduplication. Otherwise an invalid
        # model flag can block a valid deterministic coverage flag and then be
        # removed itself, losing the fact entirely.
        result: list[str] = []
        for candidate in _sanitize_flags(base, positive=allow_subset):
            candidate_terms = dedup_terms(candidate)
            if candidate_terms and any(
                candidate_terms == dedup_terms(existing) for existing in result
            ):
                continue
            result.append(candidate)
        for candidate in additions:
            candidate_terms = dedup_terms(candidate)
            duplicate = False
            for existing in result:
                existing_terms = dedup_terms(existing)
                equivalent = candidate_terms and existing_terms and candidate_terms == existing_terms
                covered_green = (
                    allow_subset
                    and candidate_terms
                    and existing_terms
                    and candidate_terms <= existing_terms
                )
                if equivalent or covered_green:
                    duplicate = True
                    break
            if not duplicate:
                result.append(candidate)
        return _sanitize_flags(result, positive=allow_subset)

    merged_green = merge(green_flags, requested_green, allow_subset=True)
    # Red coverage remains atomic: a compound model statement must never hide
    # an independently enforceable Senior or >3 years prohibition.
    merged_red = merge(red_flags, requested_red, allow_subset=False)
    # An explicit negative predicate wins over a contradictory model green
    # flag. This prevents "не интересуют продажи" from becoming green merely
    # because the model copied the noun into its positive list.
    negative_terms = [
        terms(flag)
        for flag in merged_red
        # "без X" / "не X" describes absence or an exception and must not
        # erase a separate positive preference mentioning X.
        if not re.search(r"\b(?:без|не|нет|отсутств)\b", flag, re.I)
    ]
    merged_green = [
        flag for flag in merged_green
        if not any(terms(flag) & forbidden for forbidden in negative_terms)
    ]
    # Deterministic positive coverage comes directly from explicit positive
    # clauses in the request. Re-merge it after model-conflict cleanup so a
    # noisy model red flag cannot erase an independently requested domain
    # such as consulting.
    merged_green = merge(merged_green, requested_green, allow_subset=True)
    return merged_green, merged_red


def _legacy_flags(request_text: str) -> tuple[list[str], list[str]]:
    """Small, deterministic migration fallback when no model is available."""
    text = request_text.strip()
    green: list[str] = []
    red: list[str] = []
    for pattern in (r"\bв области\s+([^,.!;]+)", r"\bв сфере\s+([^,.!;]+)", r"\bинтересуют\s+([^,.!;]+)"):
        green.extend(f"Вакансия связана с {match.strip()}" for match in re.findall(pattern, text, re.I))
    for pattern in (r"\bбез\s+([^,.!;]+)", r"\bне интересуют\s+([^,.!;]+)", r"\bне рассматриваю\s+([^,.!;]+)"):
        red.extend(f"Вакансия связана с {match.strip()}" for match in re.findall(pattern, text, re.I))
    # Avoid repeating a flag when clauses overlap.
    return list(dict.fromkeys(green)), list(dict.fromkeys(red))


def compile_policy(request_text: str, score_threshold: int = 75) -> SearchPolicy:
    """Synchronous legacy compiler used for migrations and unavailable models."""
    green, red = _legacy_flags(request_text)
    green, red = _merge_request_coverage(request_text, green, red)
    return SearchPolicy(
        request_text=request_text.strip(),
        score_threshold=score_threshold,
        green_flags=_sanitize_flags(green, positive=True),
        red_flags=_sanitize_flags(red),
    )


async def compile_policy_with_ai(
    request_text: str, score_threshold: int = 75, gateway=None
) -> SearchPolicy:
    """Compile a policy through the dedicated structured AI role.

    The sync compiler remains intentionally available for old callers and DB rows
    created before flag extraction existed.
    """
    from backend.intelligence.gateway import ModelGateway
    from backend.schemas.domain import PolicyCompilation

    gateway = gateway or ModelGateway()
    parsed = await gateway.structured(
        "policy_compiler",
        {"request_text": request_text.strip(), "score_threshold": score_threshold},
        PolicyCompilation,
    )
    green_flags, red_flags = _merge_request_coverage(
        request_text, parsed.green_flags, parsed.red_flags
    )
    return SearchPolicy(
        request_text=request_text.strip(),
        score_threshold=score_threshold,
        green_flags=green_flags,
        red_flags=red_flags,
        # Confidence policy is an application invariant, not model output.
        flag_confidence_threshold=0.70,
    )
