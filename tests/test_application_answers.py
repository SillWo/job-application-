import pytest

from backend.adapters.base.protocol import ApplicationForm, FillResult
from backend.intelligence.application_answers import _taxes, prepare_answers, resolve_salary
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
@pytest.mark.parametrize(("experience", "condition", "accepted"), [
    ("Опыт работы: 3–6 лет", "офис, требуемый опыт более 3 лет", False),
    ("Опыт работы: 3–6 лет", "офис, требуемый опыт 3 года и меньше", False),
    ("Опыт работы: 1–3 года", "офис, требуемый опыт 3 года и меньше", True),
    ("Опыт работы: 4–6 лет", "офис, требуемый опыт более 3 лет", True),
    ("Опыт работы: 3–6 лет", "офис, для диапазона 3–6 лет", True),
])
async def test_model_cannot_select_salary_across_experience_boundary(experience, condition, accepted):
    text = f"120000 RUB — {condition}"
    gateway = Gateway(dict(has_salary_rules=True, rules=[rule(120000, text, condition)]),
                      dict(rule_index=0, context_complete=True, confidence=1, vacancy_evidence=[experience], reason="Условия подходят"))
    if not accepted:
        gateway.responses.append(dict(amount=120000, currency="RUB", gross=None, period="month",
                                      confidence=.8, evidence=[condition], reason="Оценка при пересечении опыта"))
    result = await resolve_salary(gateway, job(required_experience=experience), [], text)
    assert result.rule is not None
    assert (result.source == "preferences") is accepted


@pytest.mark.asyncio
@pytest.mark.parametrize("selection", [
    dict(rule_index=None, context_complete=False, confidence=0, vacancy_evidence=[], reason="Город неизвестен"),
    dict(rule_index=0, context_complete=True, confidence=1, vacancy_evidence=["Москва"], reason="Город выдуман"),
    dict(rule_index=99, context_complete=True, confidence=1, vacancy_evidence=["Разработчик"], reason="Неверный индекс"),
])
async def test_unmatched_or_ungrounded_rule_uses_estimate_without_flattening_resume(selection):
    gateway = Gateway(dict(has_salary_rules=True, rules=[rule(250000, "офис МСК 250к", "Москва, офис")]), selection,
                      dict(amount=160000, currency="RUB", gross=None, period="month", confidence=.8,
                           evidence=["Оценка по контексту"], reason="Примерная оценка по выбранному резюме"))
    result = await resolve_salary(gateway, job(), [{"desired_salary": "150000 RUB"}], "офис МСК 250к")
    assert result.source == "estimate"
    assert result.rule.amount == 160000
    assert gateway.calls[-1][0] == "application_salary_estimate"
    assert gateway.calls[-1][1]["resumes"] == [{"desired_salary": "150000 RUB"}]


@pytest.mark.asyncio
@pytest.mark.parametrize("rules", [[], [rule(777777, "150000 RUB")], [rule(150000, "нет такой цитаты")]])
async def test_invalid_preference_rules_use_grounded_estimate(rules):
    gateway = Gateway(dict(has_salary_rules=True, rules=rules),
                      dict(amount=145000, currency="RUB", gross=None, period="month", confidence=.8,
                           evidence=["Зарплата 150000 RUB"], reason="Оценка по исходному ориентиру"))
    result = await resolve_salary(gateway, job(), [{"desired_salary": "120000 RUB"}], "Зарплата 150000 RUB")
    assert result.source == "estimate"
    assert result.rule.amount == 145000
    assert gateway.calls[-1][1]["salary_rules"] == [] or all(
        rule_item["amount"] == 150000 for rule_item in gateway.calls[-1][1]["salary_rules"]
    )


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
@pytest.mark.parametrize(("label", "profile", "value", "source"), [
    ("Будет здорово, если вы оставите свой ник в tg", {"contacts": {"telegram": "@example"}}, "@example", "profile.contacts.telegram"),
    ("Имеете ли Вы опыт в финтехе, эквайринге, платёжных или банковских системах?",
     {"about": "Разрабатывал банковские системы"}, "Разрабатывал банковские системы", "profile.about"),
])
async def test_banking_experience_and_colloquial_health_word_are_not_sensitive(label, profile, value, source):
    answer = {**proposal(values=[value]), "evidence": [{"source": source, "quote": value}]}
    plan = await answer_form(ApplicationField(id="q1", label=label), [answer], profile=profile)
    assert plan.form_answers["q1"].values == [value]


@pytest.mark.asyncio
@pytest.mark.parametrize("label", ["Ваши банковские реквизиты", "Номер банковского счёта", "Номер карты", "Состояние здоровья", "Ваш диагноз", "Your bank account number"])
async def test_actual_health_and_banking_details_remain_sensitive(label):
    plan = await answer_form(ApplicationField(id="q1", label=label), [proposal()])
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
    responses = [dict(has_salary_rules=True, rules=[rule(200000, "200000 RUB")])]
    if label != "Желаемая зарплата":
        responses.append(dict(
            amount=200000,
            currency="USD" if "USD" in label else "RUB",
            gross=True if "до вычета" in label else False if "на руки" in label else None,
            period="year" if "в год" in label else "month",
            confidence=.8,
            evidence=["Оценка в базе вопроса"],
            reason="Оценка в запрошенной базе",
        ))
    responses.append(dict(answers=[answer]))
    gateway = Gateway(*responses)
    plan = await answer_form(ApplicationField(id="q1", label=label), [], gateway=gateway,
                             resumes=[{"desired_salary": "200000 RUB"}])
    expected = "в год" in label or "до вычета" in label
    assert bool(plan.form_answers) is expected


@pytest.mark.asyncio
async def test_salary_compiler_cannot_invent_tax_basis():
    salary_rule = {**rule(), "gross": False}
    gateway = Gateway(dict(has_salary_rules=True, rules=[salary_rule]))
    result = await resolve_salary(gateway, job(), [{"desired_salary": "150000 RUB"}], "")
    assert result.rule.amount == 150000
    assert result.rule.gross is None
    assert "на руки" not in result.display()


@pytest.mark.asyncio
@pytest.mark.parametrize(("label", "accepted"), [("Желаемая зарплата", True), ("Желаемая зарплата на руки", True)])
async def test_normalized_unknown_taxes_allow_only_unspecified_tax_question(label, accepted):
    answer = {**proposal("salary", ["150000 RUB"]), "evidence": [{"source": "salary", "quote": "150000 RUB"}]}
    responses = [dict(has_salary_rules=True, rules=[{**rule(), "gross": False}])]
    if "на руки" in label:
        responses.append(dict(amount=150000, currency="RUB", gross=False, period="month", confidence=.8,
                              evidence=["Оценка на руки"], reason="Оценка после налогов"))
    responses.append(dict(answers=[answer]))
    gateway = Gateway(*responses)
    plan = await answer_form(ApplicationField(id="q1", label=label), [], gateway=gateway,
                             resumes=[{"desired_salary": "150000 RUB"}])
    assert bool(plan.form_answers) is accepted


def test_after_tax_phrase_is_detected_as_net_basis():
    assert _taxes("Какие у вас зарплатные ожидания? (сумма после налогов)") == {False}


@pytest.mark.asyncio
async def test_exact_match_requires_one_hundred_percent_confidence():
    text = "Офис МСК 100000 RUB"
    gateway = Gateway(
        dict(has_salary_rules=True, rules=[rule(100000, text, "офис Москва")]),
        dict(rule_index=0, context_complete=True, confidence=.99, vacancy_evidence=["Москва", "офис"], reason="Почти совпало"),
        dict(amount=110000, currency="RUB", gross=None, period="month", confidence=.8,
             evidence=["Оценка"], reason="Оценка при неполной уверенности"),
    )
    result = await resolve_salary(gateway, job(location="Москва", work_format="офис"),
                                  [{"desired_salary": "100000 RUB"}], text)
    assert result.source == "estimate"
    assert result.rule.amount == 110000
    assert [role for role, _ in gateway.calls] == [
        "application_salary_rules", "application_salary_selection", "application_salary_estimate",
    ]


@pytest.mark.asyncio
async def test_net_question_adapts_exact_amount_when_source_tax_basis_is_unknown():
    text = "Офис МСК 120000 RUB"
    gateway = Gateway(
        dict(has_salary_rules=True, rules=[rule(120000, text, "офис Москва")]),
        dict(rule_index=0, context_complete=True, confidence=1, vacancy_evidence=["Москва", "офис"], reason="Условия совпали"),
        dict(amount=120000, currency="RUB", gross=False, period="month", confidence=.8,
             evidence=["Исходная сумма 120000 RUB"], reason="Сумма сохранена и указана после налогов"),
    )
    result = await resolve_salary(
        gateway,
        job(location="Москва", work_format="офис"),
        [{"desired_salary": "120000 RUB"}],
        text,
        question="Какие у вас зарплатные ожидания? (сумма после налогов)",
    )
    assert result.source == "estimate"
    assert result.rule.amount == 120000
    assert result.rule.gross is False
    assert gateway.calls[-1][1]["requested_context"]["gross"] is False


@pytest.mark.asyncio
async def test_salary_estimate_is_accepted_without_guaranteed_application_and_keeps_reason():
    answer = {
        "field_id": "salary",
        "category": "salary",
        "values": ["120000"],
        "evidence": [{"source": "salary_estimate", "quote": "Оценка: 120000 RUB после налогов"}],
        "confidence": .8,
        "reason": "Оценка по данным кандидата",
    }
    gateway = Gateway(
        dict(has_salary_rules=True, rules=[rule(120000, "120000 RUB")]),
        dict(amount=120000, currency="RUB", gross=False, period="month", confidence=.8,
             evidence=["Резюме"], reason="120000 RUB после налогов"),
        dict(answers=[answer]),
    )
    plan = await answer_form(
        ApplicationField(id="salary", label="Зарплатные ожидания (сумма после налогов)"),
        [], gateway=gateway, resumes=[{"desired_salary": "120000 RUB"}],
    )
    assert plan.form_answers["salary"].values == ["120000"]
    assert "Оценка:" in plan.form_answers["salary"].explanation


@pytest.mark.asyncio
async def test_rejected_salary_answer_reports_local_validation_reason():
    answer = {
        "field_id": "salary",
        "category": "salary",
        "values": ["150000 RUB"],
        "evidence": [{"source": "job.description", "quote": "150000 RUB"}],
        "confidence": 1,
        "reason": "Пользователь указал сумму",
    }
    gateway = Gateway(
        dict(has_salary_rules=True, rules=[rule(150000)]),
        dict(answers=[answer]),
    )
    plan = await answer_form(
        ApplicationField(id="salary", label="Зарплатные ожидания"),
        [], gateway=gateway, resumes=[{"desired_salary": "150000 RUB"}],
    )
    assert "150000 RUB" in plan.unanswered_fields["salary"]
    assert "salary" in plan.unanswered_fields["salary"]


@pytest.mark.asyncio
async def test_historical_office_or_hybrid_context_uses_moscow_three_to_six_rule():
    text = "Удалёнка не МСК/СПб <=3: 70000; >3: 100000; офис не МСК/СПб <=3: 80000; >3: 100000; офис МСК/СПб <=3: 100000; >=3: 120000 RUB/month"
    rules = [
        rule(70000, "<=3: 70000", "удалёнка не МСК/СПб <=3"),
        rule(100000, ">3: 100000", "удалёнка не МСК/СПб >3"),
        rule(80000, "<=3: 80000", "офис не МСК/СПб <=3"),
        rule(100000, ">3: 100000", "офис не МСК/СПб >3"),
        rule(100000, "офис МСК/СПб <=3: 100000", "офис МСК/СПб <=3"),
        rule(120000, ">=3: 120000 RUB/month", "офис МСК/СПб >=3"),
    ]
    gateway = Gateway(
        dict(has_salary_rules=True, rules=rules),
        dict(rule_index=5, context_complete=True, confidence=1, vacancy_evidence=["Москва", "гибрид", "3–6 лет"], reason="Москва и гибрид"),
    )
    result = await resolve_salary(
        gateway,
        job(location="Москва", work_format="Формат работы: на месте работодателя или гибрид",
             required_experience="Опыт работы: 3–6 лет"), [], text,
    )
    assert result.source == "preferences"
    assert result.rule.amount == 120000


@pytest.mark.asyncio
async def test_explicit_form_rules_exclude_filter_floor_and_keep_full_description():
    description = "Не рассматривать ниже 60к RUB. При заполнении вопросов о ЗП указывай: офис 80к RUB; удалённо 70к RUB"
    gateway = Gateway(dict(has_salary_rules=True, rules=[
        rule(60000, "Не рассматривать ниже 60к RUB"),
        rule(80000, "офис 80к RUB", "офис"),
        rule(70000, "удалённо 70к RUB", "удалённо"),
    ]), dict(rule_index=0, context_complete=True, confidence=1, vacancy_evidence=["офис"], reason="Офис"))
    result = await resolve_salary(gateway, job(work_format="офис"), [{"desired_salary": "999999 RUB"}], description)
    assert result.rule.amount == 80000
    assert gateway.calls[0][1]["text"] == description
    assert [r["amount"] for r in gateway.calls[1][1]["rules"]] == [80000, 70000]


@pytest.mark.asyncio
async def test_empty_form_rule_section_never_falls_back_to_filter_or_resume():
    gateway = Gateway(dict(has_salary_rules=True, rules=[rule(60000, "ниже 60к RUB")]))
    result = await resolve_salary(gateway, job(), [{"desired_salary": "999999 RUB"}],
                                  "Не брать ниже 60к RUB. При заполнении вопросов о ЗП указывай:")
    assert result.rule is None


@pytest.mark.asyncio
async def test_explicit_tax_basis_is_preserved_and_conflicting_basis_rejected():
    for gross, expected in [(True, True), (False, True), (None, True)]:
        responses = [dict(has_salary_rules=True, rules=[{**rule(150000, "150000 RUB на руки"), "gross": gross}])]
        if gross is True:
            responses.append(dict(amount=150000, currency="RUB", gross=False, period="month", confidence=.8,
                                  evidence=["150000 RUB на руки"], reason="Оценка в подтверждённой налоговой базе"))
        gateway = Gateway(*responses)
        result = await resolve_salary(gateway, job(), [], "150000 RUB на руки")
        assert (result.rule is not None) is expected


@pytest.mark.asyncio
async def test_indirect_salary_question_can_be_identified_by_model():
    initial = {**proposal("salary"), "values": [], "evidence": [], "reason": "Нужен расчёт зарплаты"}
    final = {**proposal("salary", ["150000 RUB"]), "evidence": [{"source": "salary", "quote": "150000 RUB"}]}
    gateway = Gateway(dict(answers=[initial]), dict(has_salary_rules=True, rules=[rule()]), dict(answers=[final]))
    plan = await answer_form(ApplicationField(id="q1", label="Expectativa retributiva mensual"), [],
                             gateway=gateway, resumes=[{"desired_salary": "150000 RUB"}])
    assert plan.form_answers["q1"].values == ["150000 RUB"]
    assert [role for role, _ in gateway.calls] == ["application_answers", "application_salary_rules", "application_answers"]


@pytest.mark.asyncio
@pytest.mark.parametrize("site", ["hh", "zarplata", "future_site"])
async def test_confirmed_memory_is_available_to_every_site(site):
    gateway = Gateway(dict(answers=[dict(field_id="q", category="fact", values=["B2"],
        evidence=[dict(source="memory.7", quote="B2")], confidence=1, reason="Ответ пользователя")]))
    field = ApplicationField(id="q", label="Какой у вас английский?")
    result = await prepare_answers(gateway, ApplicationForm(fields=[field]),
        ApplicationPlan(vacancy_id=1, resume_file=""), job().model_copy(update={"source": site}), {}, [], "",
        memory=[dict(id=7, question="Уровень английского", answer="B2", context={})])
    assert result.form_answers["q"].values == ["B2"]
    assert result.form_fields["q"] == field


@pytest.mark.asyncio
async def test_memory_context_cannot_leak_to_other_work_format():
    gateway = Gateway(dict(answers=[]))
    await prepare_answers(gateway, ApplicationForm(fields=[ApplicationField(id="q", label="Готовы к офису?")]),
        ApplicationPlan(vacancy_id=1, resume_file=""), job(work_format="удалённо"), {}, [], "",
        memory=[dict(id=7, question="Готовы к офису?", answer="Да", context={"work_format": "офис"})])
    assert "memory.7" not in gateway.calls[0][1]["sources"]


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_assumptions_require_explicit_session_mode_and_retain_provenance(enabled):
    gateway = Gateway(dict(answers=[dict(field_id="q", category="assumption", values=["Работал со Scrum"],
        evidence=[], confidence=.6, reason="Предположен опыт Scrum")]))
    result = await prepare_answers(gateway, ApplicationForm(fields=[ApplicationField(id="q", label="Ваш опыт Scrum?")]),
        ApplicationPlan(vacancy_id=1, resume_file=""), job(), {}, [], "", guaranteed_application=enabled)
    assert bool(result.form_answers) is enabled
    assert gateway.calls[0][1]["guaranteed_application"] is enabled
    if enabled:
        assert result.form_answers["q"].source == "assumption"
    else:
        assert "q" in result.unanswered_fields


@pytest.mark.asyncio
@pytest.mark.parametrize("label", ["Ваш пароль?", "Желаемая зарплата?", "Согласие на обработку данных"])
async def test_guaranteed_mode_does_not_override_salary_or_sensitive_guards(label):
    gateway = Gateway(dict(answers=[dict(field_id="q", category="assumption", values=["100000"],
        evidence=[], confidence=1, reason="Предположено")]))
    result = await prepare_answers(gateway, ApplicationForm(fields=[ApplicationField(id="q", label=label)]),
        ApplicationPlan(vacancy_id=1, resume_file=""), job(), {}, [], "", guaranteed_application=True)
    assert not result.form_answers


@pytest.mark.asyncio
async def test_unanswered_questions_survive_later_form_steps():
    plan = ApplicationPlan(vacancy_id=1, resume_file="")
    gateway = Gateway(dict(answers=[]), dict(answers=[]))
    for ident in ["first", "second"]:
        plan = await prepare_answers(gateway, ApplicationForm(fields=[ApplicationField(id=ident, label=ident)]),
            plan, job(), {}, [], "")
    assert set(plan.unanswered_fields) == {"first", "second"}
    assert set(plan.form_fields) == {"first", "second"}


@pytest.mark.asyncio
async def test_saved_salary_fills_missing_expectations_but_never_overrides_resume():
    memory = [dict(id=1, question="Желаемая зарплата?", answer="150000 RUB", context={})]
    gateway = Gateway(dict(has_salary_rules=True, rules=[rule()]))
    result = await resolve_salary(gateway, job(), [], "", memory)
    assert result.source == "memory"
    assert result.rule.amount == 150000
    gateway = Gateway(dict(has_salary_rules=True, rules=[rule(200000, "200000 RUB")]))
    result = await resolve_salary(gateway, job(), [{"desired_salary": "200000 RUB"}], "", memory)
    assert result.source == "resume"
    assert result.rule.amount == 200000
