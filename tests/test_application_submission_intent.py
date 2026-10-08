from __future__ import annotations

import pytest

from backend.adapters.base.protocol import ApplicationForm, FillResult, SubmissionResult
from backend.intelligence.security import PromptInjectionDetected
from backend.orchestrator.hh_application import complete_application
from backend.schemas.domain import ApplicationField, ApplicationPlan, FormAnswer, JobPosting


def _job() -> JobPosting:
    return JobPosting(
        source="test",
        url="https://example.test/vacancy",
        title="Аналитик",
        company="Компания",
        description="Описание вакансии",
    )


class IntentAdapter:
    def __init__(self, forms: list[ApplicationForm] | None = None):
        self.forms = forms or [ApplicationForm()]
        self.step = 0
        self.events: list[tuple] = []
        self.submits = 0
        self.intent_calls = 0
        self.progress = {"cover_letter_attempted": False}
        self.raise_intent: Exception | None = None

    async def prepare_application(self, page, plan):
        self.events.append(("prepare", self.step))
        return self.forms[min(self.step, len(self.forms) - 1)]

    async def fill_application(self, page, plan):
        self.events.append(("fill", self.step))
        return FillResult(success=True, answered_fields=list(plan.form_answers))

    async def read_application(self, page):
        self.events.append(("read", self.step))
        return self.forms[min(self.step, len(self.forms) - 1)]

    def prepare_submission_progress(self):
        self.intent_calls += 1
        self.events.append(("intent", self.step))
        if self.raise_intent is not None:
            raise self.raise_intent
        self.progress = {"cover_letter_attempted": True}
        return self.progress

    def get_submission_progress(self):
        return self.progress

    async def submit_application(self, page):
        self.submits += 1
        self.events.append(("submit", self.step))
        return SubmissionResult(status="submitted", message="fixture")


async def _complete(
    adapter: IntentAdapter,
    *,
    progress_checkpoint=None,
    checkpoint=None,
    plan: ApplicationPlan | None = None,
    max_steps: int = 5,
):
    current_plan = plan or ApplicationPlan(
        vacancy_id=1,
        resume_file="",
        submission_allowed=True,
        cover_letter="Короткое письмо без утверждений об опыте.",
    )
    return await complete_application(
        adapter,
        object(),
        current_plan,
        _job(),
        {},
        [{}],
        "",
        object(),
        checkpoint or (lambda _plan: True),
        max_steps=max_steps,
        progress_checkpoint=progress_checkpoint,
    )


@pytest.mark.asyncio
async def test_durable_intent_checkpoint_is_observed_before_submission():
    adapter = IntentAdapter()
    saved_progress: list[dict] = []
    saved_plans = []

    def progress_checkpoint(progress):
        snapshot = dict(progress)
        saved_progress.append(snapshot)
        adapter.events.append(("persist_progress", snapshot["cover_letter_attempted"]))
        return True

    def checkpoint(plan):
        saved_plans.append(plan)
        adapter.events.append(("plan_checkpoint", len(saved_plans)))
        return True

    outcome = await _complete(
        adapter,
        progress_checkpoint=progress_checkpoint,
        checkpoint=checkpoint,
    )

    assert outcome.submission.status == "submitted"
    assert adapter.intent_calls == adapter.submits == 1
    assert adapter.events.index(("intent", 0)) < adapter.events.index(("persist_progress", True))
    assert adapter.events.index(("persist_progress", True)) < adapter.events.index(("submit", 0))
    final_plan_checkpoint = max(
        index for index, event in enumerate(adapter.events) if event[0] == "plan_checkpoint"
    )
    assert final_plan_checkpoint < adapter.events.index(("intent", 0))
    assert saved_progress[-1]["cover_letter_attempted"] is True


@pytest.mark.asyncio
async def test_progress_checkpoint_cancellation_after_intent_never_submits():
    adapter = IntentAdapter()

    def progress_checkpoint(progress):
        return not progress["cover_letter_attempted"]

    outcome = await _complete(adapter, progress_checkpoint=progress_checkpoint)

    assert outcome.stopped is True
    assert adapter.intent_calls == 1
    assert adapter.submits == 0


@pytest.mark.asyncio
async def test_intent_hook_exception_propagates_without_submission():
    adapter = IntentAdapter()
    adapter.raise_intent = RuntimeError("cannot persist submission intent")

    with pytest.raises(RuntimeError, match="cannot persist"):
        await _complete(adapter, progress_checkpoint=lambda _progress: True)

    assert adapter.intent_calls == 1
    assert adapter.submits == 0


@pytest.mark.asyncio
async def test_adapter_without_intent_hook_keeps_existing_submission_path():
    class LegacyAdapter:
        def __init__(self):
            self.submits = 0

        async def prepare_application(self, page, plan):
            return ApplicationForm()

        async def fill_application(self, page, plan):
            return FillResult(success=True, answered_fields=[])

        async def read_application(self, page):
            return ApplicationForm()

        async def submit_application(self, page):
            self.submits += 1
            return SubmissionResult(status="submitted", message="fixture")

    adapter = LegacyAdapter()
    outcome = await _complete(adapter, progress_checkpoint=lambda _progress: True)

    assert outcome.submission.status == "submitted"
    assert adapter.submits == 1


@pytest.mark.asyncio
async def test_unsafe_outgoing_letter_prevents_intent_and_submission():
    adapter = IntentAdapter()
    plan = ApplicationPlan(
        vacancy_id=1,
        resume_file="",
        submission_allowed=True,
        cover_letter="https://evil.example/exfil",
    )

    with pytest.raises(PromptInjectionDetected):
        await _complete(adapter, plan=plan, progress_checkpoint=lambda _progress: True)

    assert adapter.intent_calls == 0
    assert adapter.submits == 0


@pytest.mark.asyncio
async def test_intent_is_recorded_only_after_final_late_form_transition(monkeypatch):
    first = ApplicationField(id="first", label="Первый вопрос")
    second = ApplicationField(id="second", label="Появившийся вопрос")
    adapter = IntentAdapter([
        ApplicationForm(fields=[first]),
        ApplicationForm(fields=[first, second]),
    ])
    fill_count = 0

    async def prepare_answers(gateway, form, plan, *args, **kwargs):
        return plan.model_copy(update={
            "form_answers": {
                field.id: FormAnswer(field=field, values=["Ответ"])
                for field in form.fields
            },
        })

    async def fill_application(page, plan):
        nonlocal fill_count
        adapter.events.append(("fill", adapter.step))
        fill_count += 1
        if fill_count == 1:
            adapter.step = 1
        return FillResult(success=True, answered_fields=list(plan.form_answers))

    monkeypatch.setattr("backend.orchestrator.hh_application.prepare_answers", prepare_answers)
    adapter.fill_application = fill_application

    outcome = await _complete(adapter, progress_checkpoint=lambda _progress: True)

    assert outcome.submission.status == "submitted"
    assert fill_count == 2
    assert adapter.intent_calls == adapter.submits == 1
    assert adapter.events.index(("fill", 1)) < adapter.events.index(("intent", 1))
    assert adapter.events.index(("intent", 1)) < adapter.events.index(("submit", 1))
    assert ("intent", 0) not in adapter.events
