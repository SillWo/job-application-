"""Grounded questionnaire answers and per-vacancy salary rules through ModelGateway."""
from __future__ import annotations

import re
from collections import Counter
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from backend.adapters.base.protocol import ApplicationForm
from backend.schemas.domain import ApplicationPlan, FormAnswer, JobPosting

SALARY_MARKERS = re.compile(r"зарплат|\bзп\b|з/п|доход|оплат|оклад|финансов\w*\s+ожидан|вознагражд|salary|compensation|pay\b|income|wage|от какой суммы рассматрива|какую сумму (?:ожида|рассматрива|хотите)", re.I)
MONEY = re.compile(r"\d[\d\s.,]*\s*(?:тыс|[кk]\b|руб|₽|\$|€|rub|usd|eur)", re.I)
SENSITIVE = re.compile(r"паспорт|снилс|инн\b|банковск|номер карт|парол|код из|здоров|религи|судим|политическ|согласие на|согласен с|passport|password|bank account|social security|agree to", re.I)
PERSONAL = re.compile(r"\b(?:вы|ваш\w*|вам|вас|ты|твой|your|you)\b|опыт|прожив|готовност|готовы|гражданств|портфолио|резюме|experience|relocat", re.I)
CURRENCIES = {
    "RUB": r"руб|₽|\brub\b|\brur\b", "USD": r"доллар|\$|\busd\b", "EUR": r"евро|€|\beur\b",
    "KZT": r"тенге|₸|\bkzt\b", "BYN": r"белорусск\w*\s+руб|\bbyn\b",
    "GBP": r"фунт|£|\bgbp\b", "CNY": r"юан|\bcny\b", "AED": r"дирхам|\baed\b",
    "GEL": r"лари|\bgel\b", "AMD": r"драм|\bamd\b", "UZS": r"\buzs\b", "KGS": r"\bkgs\b",
}


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SalaryRule(StrictModel):
    amount: int = Field(gt=0)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    gross: bool | None
    period: Literal["month", "year", "hour"]
    condition: str
    quote: str = Field(min_length=1)


class SalaryRules(StrictModel):
    has_salary_rules: bool
    rules: list[SalaryRule]


class SalarySelection(StrictModel):
    rule_index: int | None
    context_complete: bool
    confidence: float = Field(ge=0, le=1)
    vacancy_evidence: list[str]
    reason: str


class ResolvedSalary(StrictModel):
    rule: SalaryRule | None = None
    source: Literal["preferences", "resume", "unknown"] = "unknown"
    reason: str = "Зарплатные ожидания не указаны"

    def display(self) -> str:
        if self.rule is None:
            return ""
        tax = " до вычета налогов" if self.rule.gross is True else " на руки" if self.rule.gross is False else ""
        period = {"month": "месяц", "year": "год", "hour": "час"}[self.rule.period]
        return f"{self.rule.amount} {self.rule.currency} в {period}{tax}"


class AnswerEvidence(StrictModel):
    source: str
    quote: str = Field(min_length=1)


class ProposedAnswer(StrictModel):
    field_id: str
    category: Literal["fact", "preference", "salary", "knowledge", "sensitive", "task", "unknown"]
    values: list[str]
    evidence: list[AnswerEvidence]
    confidence: float = Field(ge=0, le=1)
    reason: str


class AnswerBatch(StrictModel):
    answers: list[ProposedAnswer]


def _normalized(value: str) -> str:
    return " ".join(value.casefold().replace("ё", "е").split())


def _contains(source: str, quote: str) -> bool:
    return bool(quote.strip()) and _normalized(quote) in _normalized(source)


def _amounts(text: str) -> set[int]:
    amounts = set()
    for match in re.finditer(r"(?<!\w)(\d+(?:[ \u00a0]\d{3})*(?:[.,]\d+)?)\s*(тыс\.?|[кk]\b)?", text, re.I):
        raw = re.sub(r"\s", "", match[1]).replace(",", ".")
        number = float(raw) * (1000 if match[2] else 1)
        if number.is_integer():
            amounts.add(int(number))
    return amounts


def _currencies(text: str) -> set[str]:
    codes = {code for code, pattern in CURRENCIES.items() if re.search(pattern, text, re.I)}
    if "BYN" in codes and "RUB" in codes and not re.search(r"российск|\brub\b|₽", text, re.I):
        codes.remove("RUB")
    return codes


def _taxes(text: str) -> set[bool]:
    result = set()
    if re.search(r"до\s+(?:вычета|уплаты)\s+(?:налог|ндфл)|до\s+ндфл|\bgross\b", text, re.I):
        result.add(True)
    if re.search(r"после\s+(?:вычета|уплаты)\s+(?:налог|ндфл)|на руки|\bnet\b|чистыми", text, re.I):
        result.add(False)
    return result


def _periods(text: str) -> set[str]:
    return {period for period, pattern in {"month": r"в месяц|за месяц|monthly|per month",
                                           "year": r"в год|за год|annual|per year",
                                           "hour": r"в час|за час|hourly|per hour"}.items()
            if re.search(pattern, text, re.I)}


def _valid_rule(source: str, rule: SalaryRule) -> bool:
    currency = _currencies(rule.quote) or _currencies(source)
    taxes = _taxes(rule.quote) or _taxes(source)
    periods = _periods(rule.quote) or _periods(source)
    return (bool(_contains(source, rule.quote)) and rule.amount in _amounts(rule.quote)
            and (not currency or rule.currency in currency)
            and (rule.gross is None or taxes == {rule.gross})
            and (not periods or rule.period in periods))


def _salary_matches(field, values: list[str], rule: SalaryRule) -> bool:
    text = " ".join(values)
    # The selected amount cannot be replaced or supplemented with another monetary amount.
    monetary_amounts = set().union(*(_amounts(match.group()) for match in MONEY.finditer(text)))
    return (rule.amount in _amounts(text) and monetary_amounts <= {rule.amount}
            and _currencies(field.label) <= {rule.currency} and _currencies(text) <= {rule.currency}
            and _taxes(field.label) <= {rule.gross} and _taxes(text) <= {rule.gross}
            and _periods(field.label) <= {rule.period} and _periods(text) <= {rule.period})


def _sources(profile, resumes, description: str) -> dict[str, str]:
    result: dict[str, str] = {}

    def visit(value, path):
        if hasattr(value, "model_dump"):
            value = value.model_dump(mode="json")
        if isinstance(value, dict):
            for key, item in value.items():
                # File paths and internal flags are not applicant facts.
                if key not in {"original_path", "original_filename", "selected_for_matching", "id", "profile_id"}:
                    visit(item, f"{path}.{key}")
        elif isinstance(value, list):
            for index, item in enumerate(value):
                visit(item, f"{path}.{index}")
        elif value is not None and str(value).strip():
            result[path] = str(value)

    visit(profile, "profile")
    visit(resumes, "resumes")
    if description.strip():
        result["preferences"] = description.strip()
    return result


async def resolve_salary(gateway, job: JobPosting, resumes: list[dict], description: str) -> ResolvedSalary:
    """User text is authoritative; conditional rules are never flattened to a minimum."""
    rules = SalaryRules(has_salary_rules=False, rules=[])
    source_text = description.strip()
    if source_text:
        rules = await gateway.structured("application_salary_rules", {"text": source_text}, SalaryRules)
    # A mention of paid training or company revenue is not a candidate salary rule.
    preference_salary = rules.has_salary_rules or bool(rules.rules)
    source = "preferences"
    if not preference_salary:
        salaries = list(dict.fromkeys(str(resume.get("desired_salary") or "").strip() for resume in resumes))
        salaries = [salary for salary in salaries if salary]
        if len(salaries) != 1:
            return ResolvedSalary(reason="В выбранных резюме зарплата отсутствует или различается")
        source_text = salaries[0]
        source = "resume"
        rules = await gateway.structured("application_salary_rules", {"text": source_text}, SalaryRules)
    # An invalid rule poisons the rule set: silently dropping it could select a wrong fallback.
    if not rules.rules or any(not _valid_rule(source_text, rule) for rule in rules.rules):
        return ResolvedSalary(reason="Не удалось однозначно прочитать зарплатные условия пользователя")
    if len(rules.rules) == 1 and not rules.rules[0].condition.strip():
        return ResolvedSalary(rule=rules.rules[0], source=source, reason="Явное зарплатное ожидание")
    job_payload = job.model_dump(mode="json")
    # The employer's compensation must never become the candidate's salary expectation.
    job_payload.pop("salary", None)
    selected = await gateway.structured("application_salary_selection", {
        "rules": [rule.model_dump() for rule in rules.rules],
        "job": job_payload,
        "source_text": source_text,
    }, SalarySelection)
    index = selected.rule_index
    evidence_text = " ".join(str(value) for value in job_payload.values() if value is not None)
    if (index is None or not 0 <= index < len(rules.rules) or not selected.context_complete
            or selected.confidence < .95 or not selected.vacancy_evidence
            or any(not _contains(evidence_text, quote) for quote in selected.vacancy_evidence)):
        return ResolvedSalary(reason=selected.reason or "Условия зарплатного правила не подтверждены вакансией")
    return ResolvedSalary(rule=rules.rules[index], source=source, reason=selected.reason)


async def prepare_answers(gateway, form: ApplicationForm, plan: ApplicationPlan, job: JobPosting,
                          profile, resumes: list[dict], description: str) -> ApplicationPlan:
    """Model output is a proposal; source quotes, IDs, choices and salary are checked locally."""
    if not form.fields:
        return plan
    sources = _sources(profile, resumes, description)
    salary_fields = {field.id for field in form.fields if SALARY_MARKERS.search(field.label)}
    salary = await resolve_salary(gateway, job, resumes, description) if salary_fields else ResolvedSalary()
    # Remove raw salary fields so lower-priority resume amounts cannot leak into a composite answer.
    sources = {key: value for key, value in sources.items() if not key.endswith(".desired_salary")}
    if salary.rule:
        sources["salary"] = salary.display()
    async def propose():
        return await gateway.structured("application_answers", {
            "fields": [field.model_dump() for field in form.fields],
            "sources": sources,
            "salary": salary.model_dump(),
            "as_of_date": date.today().isoformat(),
            "job": job.model_dump(mode="json"),
        }, AnswerBatch)

    proposed = await propose()
    if not salary_fields:
        # Salary questions can be indirect or in another language. Let the model
        # identify them, then resolve the same authoritative rules before answering.
        known_fields = {field.id for field in form.fields}
        salary_fields = {answer.field_id for answer in proposed.answers
                         if answer.category == "salary" and answer.field_id in known_fields}
        if salary_fields:
            salary = await resolve_salary(gateway, job, resumes, description)
            if salary.rule:
                sources["salary"] = salary.display()
            proposed = await propose()
    counts = Counter(answer.field_id for answer in proposed.answers)
    answers = {answer.field_id: answer for answer in proposed.answers if counts[answer.field_id] == 1}
    result = plan.model_copy(deep=True)
    result.unanswered_fields = {}
    for field in form.fields:
        result.form_answers.pop(field.id, None)
        answer = answers.get(field.id)
        reason = "Для ответа недостаточно подтверждённых данных"
        accepted = bool(answer and answer.values and all(value.strip() for value in answer.values)
                        and answer.confidence >= .95 and answer.category not in {"unknown", "sensitive", "task"})
        if answer:
            reason = answer.reason or reason
            if answer.category == "knowledge":
                accepted = accepted and not PERSONAL.search(field.label) and bool(answer.reason.strip())
            else:
                accepted = accepted and bool(answer.evidence) and all(
                    evidence.source in sources and _contains(sources[evidence.source], evidence.quote)
                    for evidence in answer.evidence
                )
            if field.id in salary_fields or answer.category == "salary":
                if salary.rule is None:
                    accepted = False
                    reason = salary.reason
                else:
                    accepted = accepted and any(e.source == "salary" for e in answer.evidence)
                    if not _salary_matches(field, answer.values, salary.rule):
                        accepted = False
                        reason = "Сумма, валюта, период или налоговая база ответа не совпадают с ожиданиями"
            if field.options:
                accepted = accepted and len(set(field.options)) == len(field.options) and all(value in field.options for value in answer.values)
            if field.kind not in {"checkbox", "multiselect"}:
                accepted = accepted and len(answer.values) == 1
            if field.kind == "number":
                accepted = accepted and all(re.fullmatch(r"\d+(?:\.\d+)?", value) for value in answer.values)
            if field.max_length is not None:
                accepted = accepted and all(len(value) <= field.max_length for value in answer.values)
        if SENSITIVE.search(field.label) or field.kind == "unsupported" or field.label.startswith("Вопрос без подписи"):
            accepted = False
            reason = "Вопрос требует участия пользователя"
        if accepted:
            result.form_answers[field.id] = FormAnswer(
                field=field, values=answer.values, source=answer.category, explanation=answer.reason,
            )
        else:
            result.unanswered_fields[field.id] = reason
    return result
