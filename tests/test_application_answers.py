import pytest

from backend.adapters.base.protocol import ApplicationForm, FillResult
from backend.intelligence.application_answers import prepare_answers, resolve_salary
from backend.orchestrator.application_guard import unresolved_application_questions
from backend.schemas.domain import ApplicationField, ApplicationPlan, JobPosting


def job(**kwargs):
    return JobPosting(source="hh", url="https://hh.ru/vacancy/1", title="Разработчик",
                      description="Работа над продуктом", **kwargs)


def rule(amount=150000, quote="150000 RUB", condition="", **kwargs):
    return dict(amount=amount, currency="RUB", gross=None, period="month", condition=condition,
                quote=quote, **kwargs)


class Gateway:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    async def structured(self, role, payload, schema):
        self.calls.append((role, payload))
        return schema.model_validate(self.responses.pop(0))


@pytest.mark.asyncio
async def test_salary_uses_resume_without_preferences():
    gateway = Gateway(dict(has_salary_rules=True, rules=[rule()]))
    result = await resolve_salary(gateway, job(), [{"desired_salary": "150000 RUB"}], "")
    assert result.rule.amount == 150000
    assert result.source == "resume"


@pytest.mark.asyncio
async def test_preferences_override_resume_without_even_sending_resume_salary():
    gateway = Gateway(dict(has_salary_rules=True, rules=[rule(200000, "200к RUB")]))
    result = await resolve_salary(gateway, job(), [{"desired_salary": "150000 RUB"}], "Зарплата 200к RUB")
    assert result.rule.amount == 200000
    assert result.source == "preferences"
    assert "150000" not in str(gateway.calls)


@pytest.mark.asyncio
async def test_no_salary_in_description_falls_back_to_resume():
    gateway = Gateway(dict(has_salary_rules=False, rules=[]), dict(has_salary_rules=True, rules=[rule()]))
    result = await resolve_salary(gateway, job(), [{"desired_salary": "150000 RUB"}], "Хочу работать с Python")
    assert result.rule.amount == 150000


@pytest.mark.asyncio
async def test_paid_training_is_not_a_salary_override():
    gateway = Gateway(dict(has_salary_rules=False, rules=[]), dict(has_salary_rules=True, rules=[rule()]))
    result = await resolve_salary(gateway, job(), [{"desired_salary": "150000 RUB"}], "Хочу оплату обучения")
    assert result.rule.amount == 150000


@pytest.mark.asyncio
@pytest.mark.parametrize(("format_", "index", "expected"), [("на месте работодателя", 0, 250000), ("удалённо", 1, 180000)])
async def test_conditional_salary_receives_full_job_and_unflattened_rules(format_, index, expected):
    description = "Офис МСК 250к RUB; удалённо 180к RUB"
    gateway = Gateway(dict(has_salary_rules=True, rules=[
        rule(250000, "Офис МСК 250к RUB", "Офис в Москве"),
        rule(180000, "удалённо 180к RUB", "Удалённо"),
    ]), dict(rule_index=index, context_complete=True, confidence=1, vacancy_evidence=[format_, "Москва"], reason="Все условия совпали"))
    result = await resolve_salary(gateway, job(location="Москва", work_format=format_),
                                  [{"desired_salary": "999999 RUB"}], description)
    assert result.rule.amount == expected
    payload = gateway.calls[-1][1]
    assert payload["job"]["location"] == "Москва"
    assert payload["job"]["work_format"] == format_
    assert payload["source_text"] == description
    assert "salary" not in payload["job"]


@pytest.mark.asyncio
@pytest.mark.parametrize("selection", [
    dict(rule_index=None, context_complete=False, confidence=0, vacancy_evidence=[], reason="Город неизвестен"),
    dict(rule_index=0, context_complete=True, confidence=1, vacancy_evidence=["Москва"], reason="Город выдуман"),
    dict(rule_index=99, context_complete=True, confidence=1, vacancy_evidence=["Разработчик"], reason="Неверный индекс"),
])
async def test_unmatched_or_ungrounded_rule_never_falls_back_to_resume(selection):
    gateway = Gateway(dict(has_salary_rules=True, rules=[rule(250000, "офис МСК 250к", "Москва, офис")]), selection)
    result = await resolve_salary(gateway, job(), [{"desired_salary": "150000 RUB"}], "офис МСК 250к")
    assert result.rule is None
    assert "150000" not in str(gateway.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("rules", [[], [rule(777777, "150000 RUB")], [rule(150000, "нет такой цитаты")]])
async def test_invalid_preference_rules_cannot_fall_back(rules):
    gateway = Gateway(dict(has_salary_rules=True, rules=rules))
    assert (await resolve_salary(gateway, job(), [{"desired_salary": "120000 RUB"}], "Зарплата 150000 RUB")).rule is None


@pytest.mark.asyncio
async def test_conflicting_resumes_require_clarification():
    result = await resolve_salary(Gateway(), job(), [{"desired_salary": "100000 RUB"}, {"desired_salary": "200000 RUB"}], "")
    assert result.rule is None


def proposal(category="fact", values=None, **kwargs):
    return dict(field_id="q1", category=category, values=values or ["Красноярск"],
                evidence=[{"source": "profile.residence", "quote": "Красноярск"}],
                confidence=1, reason="Город из профиля", **kwargs)


async def answer_form(field, proposals, *, profile=None, description="", gateway=None, resumes=None):
    plan = ApplicationPlan(vacancy_id=1, resume_file="", submission_allowed=True)
    form = ApplicationForm(fields=[field], questions=[field.label])
    gateway = gateway or Gateway(dict(answers=proposals))
    return await prepare_answers(gateway, form, plan, job(), profile or {"residence": "Красноярск"}, resumes or [{}], description)


@pytest.mark.asyncio
async def test_grounded_personal_answer_is_accepted():
    plan = await answer_form(ApplicationField(id="q1", label="Ваш город?"), [proposal()])
    assert plan.form_answers["q1"].values == ["Красноярск"]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [
    {"evidence": [{"source": "job.description", "quote": "Красноярск"}]},
    {"evidence": [{"source": "profile.residence", "quote": "Москва"}]},
    {"confidence": .8}, {"category": "knowledge"}, {"category": "unknown"},
    {"field_id": "invented"}, {"category": "task"},
])
async def test_untrusted_personal_answers_are_rejected(change):
    answer = {**proposal(), **change}
    plan = await answer_form(ApplicationField(id="q1", label="Ваш город?"), [answer])
    assert not plan.form_answers
    assert plan.unanswered_fields


@pytest.mark.asyncio
async def test_logical_quiz_can_be_answered_without_inventing_resume_facts():
    answer = {**proposal("knowledge", ["Нет, это не следует из утверждений"]), "evidence": [], "reason": "Множества могут не пересекаться"}
    plan = await answer_form(ApplicationField(id="q1", label="Все кошки любят рыбу. Некоторые любители рыбы — рыбаки. Следует ли, что некоторые кошки — рыбаки?"), [answer])
    assert plan.form_answers["q1"].source == "knowledge"


@pytest.mark.asyncio
@pytest.mark.parametrize("field", [
    ApplicationField(id="q1", label="Согласие на обработку данных"),
    ApplicationField(id="q1", label="Номер паспорта"),
    ApplicationField(id="q1", label="Ваш город?", kind="select", options=["Москва", "Казань"]),
    ApplicationField(id="q1", label="Ваш город?", max_length=3),
    ApplicationField(id="q1", label="Вопрос без подписи (q1)"),
])
async def test_sensitive_unknown_and_invalid_controls_require_review(field):
    plan = await answer_form(field, [proposal()])
    assert not plan.form_answers


@pytest.mark.asyncio
async def test_duplicate_model_answer_ids_fail_closed():
    plan = await answer_form(ApplicationField(id="q1", label="Ваш город?"), [proposal(), proposal()])
    assert not plan.form_answers


@pytest.mark.asyncio
async def test_salary_answer_cannot_use_resume_instead_of_preferences():
    answer = {**proposal("salary", ["150000 RUB"]), "evidence": [{"source": "salary", "quote": "200000 RUB"}]}
    gateway = Gateway(dict(has_salary_rules=True, rules=[rule(200000, "200000 RUB")]), dict(answers=[answer]))
    plan = await answer_form(ApplicationField(id="q1", label="Желаемая зарплата?"), [],
                             description="Зарплата 200000 RUB", gateway=gateway,
                             resumes=[{"desired_salary": "150000 RUB"}])
    assert not plan.form_answers


@pytest.mark.asyncio
async def test_composite_salary_question_is_blocked_when_salary_unknown():
    gateway = Gateway(dict(answers=[proposal()]))
    plan = await answer_form(ApplicationField(id="q1", label="Ваш город и зарплатные ожидания?"), [], gateway=gateway)
    assert not plan.form_answers


def test_guard_requires_proof_per_field_even_with_duplicate_labels():
    form = ApplicationForm(fields=[ApplicationField(id="1", label="Ответ"), ApplicationField(id="2", label="Ответ")], questions=["Ответ"])
    assert unresolved_application_questions(form, FillResult(success=True, answered_fields=["1"])) == ["Ответ"]
    assert not unresolved_application_questions(form, FillResult(success=True, answered_fields=["1", "2"]))
    assert unresolved_application_questions(ApplicationForm(), FillResult(success=False))
    assert unresolved_application_questions(ApplicationForm(questions=["Вопрос"]), FillResult(success=True)) == ["Вопрос"]


@pytest.mark.asyncio
@pytest.mark.parametrize(("label", "value"), [
    ("Зарплата в USD", "200000"),
    ("Желаемая зарплата", "200000 USD"),
    ("Желаемая зарплата в год", "200000 RUB"),
    ("Желаемая зарплата до вычета НДФЛ", "200000 RUB"),
    ("Желаемая зарплата", "200000 RUB на руки"),
    ("Желаемая зарплата", "200000 RUB или 300000 RUB"),
])
async def test_salary_cannot_change_currency_period_tax_basis_or_offer_second_amount(label, value):
    answer = {**proposal("salary", [value]), "evidence": [{"source": "salary", "quote": "200000 RUB"}]}
    gateway = Gateway(dict(has_salary_rules=True, rules=[rule(200000, "200000 RUB")]), dict(answers=[answer]))
    plan = await answer_form(ApplicationField(id="q1", label=label), [], gateway=gateway,
                             resumes=[{"desired_salary": "200000 RUB"}])
    assert not plan.form_answers


@pytest.mark.asyncio
async def test_salary_compiler_cannot_invent_tax_basis():
    salary_rule = {**rule(), "gross": False}
    gateway = Gateway(dict(has_salary_rules=True, rules=[salary_rule]))
    result = await resolve_salary(gateway, job(), [{"desired_salary": "150000 RUB"}], "")
    assert result.rule is None


@pytest.mark.asyncio
async def test_indirect_salary_question_can_be_identified_by_model():
    initial = {**proposal("salary"), "values": [], "evidence": [], "reason": "Нужен расчёт зарплаты"}
    final = {**proposal("salary", ["150000 RUB"]), "evidence": [{"source": "salary", "quote": "150000 RUB"}]}
    gateway = Gateway(dict(answers=[initial]), dict(has_salary_rules=True, rules=[rule()]), dict(answers=[final]))
    plan = await answer_form(ApplicationField(id="q1", label="Expectativa retributiva mensual"), [],
                             gateway=gateway, resumes=[{"desired_salary": "150000 RUB"}])
    assert plan.form_answers["q1"].values == ["150000 RUB"]
    assert [role for role, _ in gateway.calls] == ["application_answers", "application_salary_rules", "application_answers"]
