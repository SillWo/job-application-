import pytest

from backend.adapters.base.protocol import ApplicationForm
from backend.intelligence.application_answers import prepare_answers, resolve_salary
from backend.schemas.domain import ApplicationField, ApplicationPlan, JobPosting

EXACT_ONLY = "Зарплата: только точные правила; без оценки."


def job(**overrides):
    values = {
        "source": "hh",
        "url": "https://hh.ru/vacancy/42",
        "title": "Backend разработчик",
        "description": "Работа над продуктом",
    }
    values.update(overrides)
    return JobPosting(**values)


def salary_rule(amount, quote, condition, *, gross=None):
    return {
        "amount": amount,
        "currency": "RUB",
        "gross": gross,
        "period": "month",
        "condition": condition,
        "quote": quote,
    }


class Gateway:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    async def structured(self, role, payload, schema):
        self.calls.append((role, payload))
        return schema.model_validate(self.responses.pop(0))


def selected(index, *evidence):
    return {
        "rule_index": index,
        "context_complete": True,
        "confidence": 1,
        "vacancy_evidence": list(evidence),
        "reason": "Условия зарплатного правила подтверждены вакансией",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("location", "work_format", "required_experience", "selected_index"),
    [
        ("Москва", "гибрид", "Опыт работы: 4 года", 0),
        ("Москва", "удалённо", "Опыт работы: 4 года", 0),
        ("Красноярск", "офис", "Опыт работы: 4 года", 0),
        ("Москва", "офис", "Опыт работы: 3–6 лет", 1),
    ],
)
async def test_exact_only_rejects_mismatched_or_ambiguous_salary_context(
    location, work_format, required_experience, selected_index
):
    office = "Офис Москва; более 3 лет: 100000 RUB"
    remote = "Удалённо Москва; более 3 лет: 80000 RUB"
    outside_capitals = "Офис вне Москвы и Санкт-Петербурга; более 3 лет: 70000 RUB"
    description = f"{EXACT_ONLY}\n{office}; {remote}; {outside_capitals}"
    gateway = Gateway(
        {
            "has_salary_rules": True,
            "rules": [
                salary_rule(100000, office, "Офис Москва, опыт более 3 лет"),
                salary_rule(80000, remote, "Удалённо Москва, опыт более 3 лет"),
                salary_rule(70000, outside_capitals, "Офис вне Москвы и Санкт-Петербурга, опыт более 3 лет"),
            ],
        },
        selected(selected_index, "Москва", "офис", "более 3 лет"),
    )

    result = await resolve_salary(
        gateway,
        job(location=location, work_format=work_format, required_experience=required_experience),
        [{"desired_salary": "250000 RUB"}],
        description,
    )

    assert result.rule is None
    assert result.source == "unknown"
    assert "application_salary_estimate" not in [role for role, _ in gateway.calls]


@pytest.mark.asyncio
async def test_exact_three_year_boundary_uses_less_than_three_user_rule():
    low = "Москва, офис, опыт 3 года и меньше: 100000 RUB"
    high = "Москва, офис, опыт более 3 лет: 120000 RUB"
    description = f"{EXACT_ONLY}\n{low}; {high}"
    gateway = Gateway(
        {
            "has_salary_rules": True,
            "rules": [
                salary_rule(100000, low, "Москва офис 3 года и меньше"),
                salary_rule(120000, high, "Москва офис более 3 лет"),
            ],
        },
        selected(0, "Москва", "офис", "Опыт работы: 3 года"),
    )

    result = await resolve_salary(
        gateway,
        job(location="Москва", work_format="офис", required_experience="Опыт работы: 3 года"),
        [],
        description,
    )

    assert result.source == "preferences"
    assert result.rule.amount == 100000
    assert "application_salary_estimate" not in [role for role, _ in gateway.calls]


@pytest.mark.asyncio
@pytest.mark.parametrize(("work_format", "expected_amount"), [("удалённо", 70000), ("офис", 80000)])
async def test_exact_three_year_boundary_uses_grounded_noncapital_salary_band(work_format, expected_amount):
    rules_text = (
        "Удалённо вне МСК/СПб <=3 лет: 70000 RUB; удалённо вне МСК/СПб >3 лет: 100000 RUB; "
        "офис вне МСК/СПб <=3 лет: 80000 RUB; офис вне МСК/СПб >3 лет: 100000 RUB"
    )
    quotes = [
        "Удалённо вне МСК/СПб <=3 лет: 70000 RUB",
        "Удалённо вне МСК/СПб >3 лет: 100000 RUB",
        "офис вне МСК/СПб <=3 лет: 80000 RUB",
        "офис вне МСК/СПб >3 лет: 100000 RUB",
    ]
    amount_index = 0 if work_format == "удалённо" else 2
    amount = expected_amount
    gateway = Gateway(
        {"has_salary_rules": True, "rules": [
            salary_rule(70000, quotes[0], "удалённо вне МСК/СПб <=3 лет"),
            salary_rule(100000, quotes[1], "удалённо вне МСК/СПб >3 лет"),
            salary_rule(80000, quotes[2], "офис вне МСК/СПб <=3 лет"),
            salary_rule(100000, quotes[3], "офис вне МСК/СПб >3 лет"),
        ]},
        selected(amount_index, "Красноярск", work_format, "Опыт работы: 3 года"),
    )

    result = await resolve_salary(
        gateway,
        job(location="Красноярск", work_format=work_format, required_experience="Опыт работы: 3 года"),
        [],
        f"{EXACT_ONLY}\n{rules_text}",
    )

    assert result.rule is not None
    assert result.rule.amount == amount
    assert result.source == "preferences"


@pytest.mark.asyncio
async def test_conflicting_explicit_experience_requirements_remain_unknown():
    text = "Удалённо Москва, опыт более 3 лет: 80000 RUB"
    gateway = Gateway(
        {"has_salary_rules": True, "rules": [salary_rule(80000, text, "удалённо Москва опыт более 3 лет")]},
        selected(0, "Москва", "удалённо", "от 4 лет"),
    )

    result = await resolve_salary(
        gateway,
        job(
            location="Москва",
            work_format="удалённо",
            required_experience="Опыт работы: 3–6 лет",
            description="Требуется опыт работы от 4 лет. Необходим опыт работы не менее 5 лет.",
        ),
        [],
        f"{EXACT_ONLY}\n{text}",
    )

    assert result.rule is None
    assert "application_salary_estimate" not in [role for role, _ in gateway.calls]


@pytest.mark.asyncio
async def test_full_description_requirement_can_narrow_card_range_to_over_three_rule():
    lower = "Удалённо вне Москвы и Санкт-Петербурга, до 3 лет: 70000 RUB"
    upper = "Удалённо вне Москвы и Санкт-Петербурга, более 3 лет: 80000 RUB"
    description = f"{EXACT_ONLY}\n{lower}; {upper}\nВ вакансии требуется опыт работы от 4 лет."
    gateway = Gateway(
        {
            "has_salary_rules": True,
            "rules": [
                salary_rule(70000, lower, "Удалённо, опыт до 3 лет"),
                salary_rule(80000, upper, "Удалённо, опыт более 3 лет"),
            ],
        },
        selected(1, "Удалённо", "Красноярск", "от 4 лет"),
    )

    result = await resolve_salary(
        gateway,
        job(
            location="Красноярск",
            work_format="удалённо",
            required_experience="Опыт работы: 3–6 лет",
            description="Требуется опыт работы от 4 лет.",
        ),
        [],
        description,
    )

    assert result.source == "preferences"
    assert result.rule.amount == 80000


@pytest.mark.asyncio
async def test_explicit_noncapital_rule_does_not_match_a_capital_remote_vacancy():
    outside = "Удалённо вне Москвы и Санкт-Петербурга, более 3 лет: 70000 RUB"
    description = f"{EXACT_ONLY}\n{outside}"
    gateway = Gateway(
        {"has_salary_rules": True, "rules": [salary_rule(70000, outside, "удалённо вне Москвы и Санкт-Петербурга, более 3 лет")]},
        selected(0, "Москва", "удалённо", "опыт более 3 лет"),
    )

    result = await resolve_salary(
        gateway,
        job(location="Москва", work_format="удалённо", required_experience="Опыт работы: от 4 лет"),
        [],
        description,
    )

    assert result.rule is None
    assert "application_salary_estimate" not in [role for role, _ in gateway.calls]


@pytest.mark.asyncio
@pytest.mark.parametrize("location", ["Не указан", "Любой город", "Удалённо", "Вся Россия", "Европа"])
async def test_generic_location_does_not_confirm_noncapital_salary_rule(location):
    low = "Офис вне Москвы и Санкт-Петербурга, до 3 лет: 70000 RUB"
    high = "Офис вне Москвы и Санкт-Петербурга, более 3 лет: 100000 RUB"
    gateway = Gateway(
        {"has_salary_rules": True, "rules": [
            salary_rule(70000, low, "офис вне Москвы и Санкт-Петербурга до 3 лет"),
            salary_rule(100000, high, "офис вне Москвы и Санкт-Петербурга более 3 лет"),
        ]},
        selected(0, location, "офис", "3 года"),
    )

    result = await resolve_salary(
        gateway,
        job(location=location, work_format="офис", required_experience="Опыт работы: 3 года"),
        [],
        f"{EXACT_ONLY}\n{low}; {high}",
    )

    assert result.rule is None
    assert "application_salary_estimate" not in [role for role, _ in gateway.calls]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "experience",
    ["более 3 лет", ">3 лет", "свыше 3 лет", "от 4 лет", "не менее 4 лет"],
)
async def test_explicitly_more_than_three_years_selects_over_three_salary_rule(experience):
    low = "Офис вне Москвы и Санкт-Петербурга, до 3 лет: 80000 RUB"
    high = "Офис вне Москвы и Санкт-Петербурга, более 3 лет: 100000 RUB"
    gateway = Gateway(
        {"has_salary_rules": True, "rules": [
            salary_rule(80000, low, "офис вне Москвы и Санкт-Петербурга до 3 лет"),
            salary_rule(100000, high, "офис вне Москвы и Санкт-Петербурга более 3 лет"),
        ]},
        selected(1, "Красноярск", "офис", experience),
    )

    result = await resolve_salary(
        gateway,
        job(location="Красноярск", work_format="офис", required_experience=experience),
        [],
        f"{EXACT_ONLY}\n{low}; {high}",
    )

    assert result.rule is not None
    assert result.rule.amount == 100000


@pytest.mark.asyncio
async def test_from_three_years_remains_crossing_range_for_salary_selection():
    low = "Офис вне Москвы и Санкт-Петербурга, до 3 лет: 80000 RUB"
    high = "Офис вне Москвы и Санкт-Петербурга, более 3 лет: 100000 RUB"
    gateway = Gateway(
        {"has_salary_rules": True, "rules": [
            salary_rule(80000, low, "офис вне Москвы и Санкт-Петербурга до 3 лет"),
            salary_rule(100000, high, "офис вне Москвы и Санкт-Петербурга более 3 лет"),
        ]},
        selected(0, "Красноярск", "офис", "от 3 лет"),
    )

    result = await resolve_salary(
        gateway,
        job(location="Красноярск", work_format="офис", required_experience="от 3 лет"),
        [],
        f"{EXACT_ONLY}\n{low}; {high}",
    )

    assert result.rule is None
    assert "application_salary_estimate" not in [role for role, _ in gateway.calls]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("card_experience", "selection_index", "expected_amount"),
    [("Опыт работы: 1–3 года", 0, None), ("Опыт работы: 3–6 лет", 1, 100000)],
)
async def test_full_description_strictly_more_than_three_years_overrides_card_overlap(
    card_experience, selection_index, expected_amount
):
    low = "Офис вне Москвы и Санкт-Петербурга, до 3 лет: 80000 RUB"
    high = "Офис вне Москвы и Санкт-Петербурга, более 3 лет: 100000 RUB"
    gateway = Gateway(
        {"has_salary_rules": True, "rules": [
            salary_rule(80000, low, "офис вне Москвы и Санкт-Петербурга до 3 лет"),
            salary_rule(100000, high, "офис вне Москвы и Санкт-Петербурга более 3 лет"),
        ]},
        selected(selection_index, "Красноярск", "офис", "Требуется опыт более 3 лет"),
    )

    result = await resolve_salary(
        gateway,
        job(
            location="Красноярск",
            work_format="офис",
            required_experience=card_experience,
            description="Требуется опыт более 3 лет.",
        ),
        [],
        f"{EXACT_ONLY}\n{low}; {high}",
    )

    assert (result.rule.amount if result.rule else None) == expected_amount
    assert "application_salary_estimate" not in [role for role, _ in gateway.calls]


@pytest.mark.asyncio
@pytest.mark.parametrize("directive", [
    EXACT_ONLY,
    "Не придумывай и не оценивай обязательную зарплату.",
])
async def test_exact_only_never_falls_back_to_resume_for_missing_explicit_rule(directive):
    gateway = Gateway({"has_salary_rules": False, "rules": []})

    result = await resolve_salary(
        gateway,
        job(),
        [{"desired_salary": "150000 RUB"}],
        f"{directive}\nВ точных правилах нет подходящего значения.",
    )

    assert result.rule is None
    assert result.source == "unknown"
    assert [role for role, _ in gateway.calls] == ["application_salary_rules"]


@pytest.mark.asyncio
async def test_exact_only_does_not_use_employer_compensation_or_invent_tax_basis():
    rule_text = "Офис Москва: 100000 RUB"
    description = f"{EXACT_ONLY}\n{rule_text}"
    gateway = Gateway(
        {"has_salary_rules": True, "rules": [salary_rule(100000, rule_text, "офис Москва")]},
        selected(0, "Москва", "офис"),
    )

    result = await resolve_salary(
        gateway,
        job(location="Москва", work_format="офис", salary={"amount": 400000, "currency": "RUB", "period": "month"}),
        [],
        description,
        question="Зарплатные ожидания до вычета налогов",
    )

    assert result.rule is None
    selection_payload = gateway.calls[1][1]
    assert "salary" not in selection_payload["job"]
    assert "application_salary_estimate" not in [role for role, _ in gateway.calls]


@pytest.mark.asyncio
async def test_exact_only_keeps_unknown_salary_unanswered_without_blocking_other_fields():
    answer = {
        "field_id": "city",
        "category": "fact",
        "values": ["Красноярск"],
        "evidence": [{"source": "profile.residence", "quote": "Красноярск"}],
        "confidence": 1,
        "reason": "Город указан в профиле",
    }
    salary_text = "Удалённо Москва: 70000 RUB"
    gateway = Gateway(
        {"has_salary_rules": True, "rules": [salary_rule(70000, salary_text, "удалённо Москва")]},
        selected(0, "Москва", "удалённо"),
        {"answers": [answer]},
    )
    form = ApplicationForm(fields=[
        ApplicationField(id="salary", label="Желаемая зарплата"),
        ApplicationField(id="city", label="Город проживания"),
    ])
    plan = ApplicationPlan(vacancy_id=1, resume_file="", submission_allowed=True)

    result = await prepare_answers(
        gateway,
        form,
        plan,
        job(location="Москва", work_format="гибрид"),
        {"residence": "Красноярск"},
        [],
        f"{EXACT_ONLY}\n{salary_text}",
    )

    assert "salary" not in result.form_answers
    assert "salary" in result.unanswered_fields
    assert "city" in result.form_answers
    assert "application_salary_estimate" not in [role for role, _ in gateway.calls]
