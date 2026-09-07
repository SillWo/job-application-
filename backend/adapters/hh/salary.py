"""Parse the visible HH compensation block, without guessing monthly income."""

import re

from backend.schemas.domain import Salary


def parse_salary(text: str | None) -> Salary | None:
    if not text:
        return None
    normalized = " ".join(text.casefold().split())
    # Salary is consumed as monthly pay. Keep other periods in the original
    # description, but never compare an hourly/shift/year amount to a month.
    if re.search(r"(?:в|за)\s+(?:час|смен|день|недел|год)|/(?:ч\b|час|смен|день|нед|год)", normalized):
        return None
    currency = next((code for pattern, code in (
        (r"\bbyn\b|белорус|бел\.?\s*руб", "BYN"),
        (r"₽|руб|\brur\b|\brub\b", "RUB"),
        (r"\$|\busd\b", "USD"),
        (r"€|\beur\b", "EUR"),
        (r"₸|тенге|\bkzt\b", "KZT"),
    ) if re.search(pattern, normalized)), None)
    if currency is None:
        return None
    amount = r"\d+(?:[ ]\d{3})*(?:[.,]\d+)?(?:\s*(?:тыс\.?|[kк]\b))?"
    match = re.match(
        rf"\s*(?P<prefix>от|до)?\s*(?P<first>{amount})"
        rf"(?:\s*(?:–|—|-|до)\s*(?P<second>{amount}))?", normalized,
    )
    if not match:
        return None

    def value(raw: str) -> int:
        thousands = bool(re.search(r"тыс|[kк]", raw))
        number = re.sub(r"тыс\.?|[kк]|\s", "", raw).replace(",", ".")
        return int(float(number) * (1000 if thousands else 1))

    first = value(match["first"])
    second = value(match["second"]) if match["second"] else None
    minimum = None if match["prefix"] == "до" else first
    maximum = second if second is not None else (first if match["prefix"] != "от" else None)
    if second is not None and first > second:
        return None
    gross = True if "до вычета" in normalized else (False if "на руки" in normalized else None)
    return Salary(minimum=minimum, maximum=maximum, currency=currency, gross=gross)
