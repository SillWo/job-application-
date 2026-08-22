"""Deterministic HireHi grade selection from resume work history."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any


def _date(value: Any) -> date | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return date.fromisoformat(raw[:7] + "-01") if len(raw) == 7 else None
        except ValueError:
            return None


def _intervals(experiences: Any, today: date) -> list[tuple[date, date]]:
    result: list[tuple[date, date]] = []
    for item in experiences or []:
        if isinstance(item, dict):
            start, end = item.get("start_date"), item.get("end_date")
            current = item.get("current", False)
        else:
            start, end = getattr(item, "start_date", None), getattr(item, "end_date", None)
            current = getattr(item, "current", False)
        left = _date(start)
        right = today if current or not end else _date(end)
        if left and right and left <= right and left <= today:
            result.append((left, min(right, today)))
    result.sort()
    merged: list[list[date]] = []
    for left, right in result:
        if merged and left <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], right)
        else:
            merged.append([left, right])
    return [(left, right) for left, right in merged]


def _full_months(left: date, right: date) -> int:
    months = (right.year - left.year) * 12 + right.month - left.month
    return max(0, months - (right.day < left.day))


def hirehi_grades(resume: Any, *, today: date | None = None) -> tuple[float, list[str]]:
    """Return total non-overlapping experience in years and HireHi grades."""
    today = today or date.today()
    experiences = resume.get("experiences", []) if isinstance(resume, dict) else getattr(resume, "experiences", [])
    months = sum(_full_months(left, right) for left, right in _intervals(experiences, today))
    years = months / 12
    if months < 12:
        grades = ["intern"]
    elif months < 24:
        grades = ["intern", "junior"]
    elif months < 48:
        grades = ["intern", "junior", "middle"]
    elif months <= 72:
        grades = ["senior"]
    else:
        grades = ["lead", "head"]
    return years, grades
