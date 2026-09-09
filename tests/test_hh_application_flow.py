import pytest

from backend.adapters.base.protocol import ApplicationForm, FillResult, SubmissionResult
from backend.orchestrator import hh_application
from backend.schemas.domain import ApplicationField, ApplicationPlan, FormAnswer, JobPosting


class Adapter:
    def __init__(self, forms=None, submission="needs_input"):
        self.forms = forms or [ApplicationForm()]
        self.step = 0
        self.fills = 0
        self.submits = 0
        self.submission = submission

    async def prepare_application(self, page, plan):
        return self.forms[min(self.step, len(self.forms) - 1)]

    async def read_application(self, page):
        return self.forms[min(self.step, len(self.forms) - 1)]

    async def fill_application(self, page, plan):
        self.fills += 1
        return FillResult(success=True, answered_fields=list(plan.form_answers))

    async def submit_application(self, page):
        self.submits += 1
        self.step += 1
        return SubmissionResult(status=self.submission, message="fixture")


async def run(adapter, checkpoint=lambda _: True, **kwargs):
    return await hh_application.complete_application(
        adapter, object(), ApplicationPlan(vacancy_id=1, resume_file="", submission_allowed=True),
        JobPosting(source="hh", url="https://hh.ru/vacancy/1", title="Роль", description="Описание"),
        {}, [{}], "", object(), checkpoint, **kwargs)


@pytest.mark.asyncio
async def test_unchanged_form_after_submit_is_never_submitted_twice():
    adapter = Adapter()
    outcome = await run(adapter)
    assert outcome.pending
    assert adapter.submits == 1


@pytest.mark.asyncio
async def test_cancellation_after_model_prevents_fill_and_submit(monkeypatch):
    active = True

    async def prepare(gateway, form, plan, *args, **kwargs):
        nonlocal active
        active = False
        return plan

    monkeypatch.setattr(hh_application, "prepare_answers", prepare)
    adapter = Adapter()
    outcome = await run(adapter, lambda _: active)
    assert outcome.stopped
    assert adapter.fills == adapter.submits == 0


@pytest.mark.asyncio
async def test_answers_are_checkpointed_before_fill(monkeypatch):
    field = ApplicationField(id="q", label="Город")
    saved = []

    async def prepare(gateway, form, plan, *args, **kwargs):
        return plan.model_copy(update={"form_answers": {"q": FormAnswer(field=field, values=["Москва"])}})

    class CheckedAdapter(Adapter):
        async def fill_application(self, page, plan):
            assert saved[-1]["form_answers"]["q"]["values"] == ["Москва"]
            return await super().fill_application(page, plan)

    monkeypatch.setattr(hh_application, "prepare_answers", prepare)
    adapter = CheckedAdapter([ApplicationForm(fields=[field], questions=["Город"])], submission="submitted")
    outcome = await run(adapter, lambda plan: saved.append(plan.model_dump()) or True)
    assert outcome.submission.status == "submitted"


@pytest.mark.asyncio
async def test_new_conditional_control_is_answered_before_submit(monkeypatch):
    first = ApplicationField(id="first", label="Первый вопрос")
    second = ApplicationField(id="second", label="Второй вопрос")

    async def prepare(gateway, form, plan, *args, **kwargs):
        return plan.model_copy(update={"form_answers": {f.id: FormAnswer(field=f, values=["Ответ"]) for f in form.fields}})

    class ConditionalAdapter(Adapter):
        async def fill_application(self, page, plan):
            result = await super().fill_application(page, plan)
            self.step = 1
            return result

    monkeypatch.setattr(hh_application, "prepare_answers", prepare)
    adapter = ConditionalAdapter([ApplicationForm(fields=[first]), ApplicationForm(fields=[first, second])], submission="submitted")
    outcome = await run(adapter)
    assert outcome.submission.status == "submitted"
    assert adapter.fills == 2
    assert adapter.submits == 1


@pytest.mark.asyncio
async def test_step_limit_stops_endless_new_forms(monkeypatch):
    async def prepare(gateway, form, plan, *args, **kwargs):
        return plan.model_copy(update={"form_answers": {f.id: FormAnswer(field=f, values=["Ответ"]) for f in form.fields}})

    monkeypatch.setattr(hh_application, "prepare_answers", prepare)
    adapter = Adapter([ApplicationForm(fields=[ApplicationField(id=str(i), label=f"Вопрос {i}")]) for i in range(10)])
    outcome = await run(adapter, max_steps=3)
    assert outcome.pending
    assert adapter.submits == 3


@pytest.mark.asyncio
async def test_unconfirmed_country_does_not_submit():
    adapter = Adapter([ApplicationForm(confirmation="foreign_country")])
    outcome = await run(adapter)
    assert outcome.pending
    assert adapter.fills == adapter.submits == 0
