"""Bounded HH response steps; persistence and cancellation stay with the workflow."""
from __future__ import annotations

from dataclasses import dataclass, field

from backend.adapters.base.protocol import SubmissionResult
from backend.intelligence.application_answers import prepare_answers
from backend.orchestrator.application_guard import unresolved_application_questions


@dataclass
class ApplicationOutcome:
    submission: SubmissionResult | None = None
    pending: list[str] = field(default_factory=list)
    stopped: bool = False


def _signature(form) -> str:
    return form.model_dump_json(include={"fields", "questions", "confirmation"})


async def complete_application(adapter, page, plan, job, profile, resumes, description,
                               gateway, checkpoint, max_steps=5) -> ApplicationOutcome:
    submitted_forms: set[str] = set()
    for _ in range(max_steps):
        if not checkpoint(plan):
            return ApplicationOutcome(stopped=True)
        form = await adapter.prepare_application(page, plan)
        if form.confirmation:
            return ApplicationOutcome(pending=["Не удалось подтвердить отклик в другой стране"])
        signature = _signature(form)
        if signature in submitted_forms:
            return ApplicationOutcome(pending=["HH.ru оставил ту же форму после отправки; проверьте ответы и сообщения об ошибках"])
        plan = await prepare_answers(gateway, form, plan, job, profile, resumes, description)
        # Persist the generated answers before any field is changed, and recheck cancellation
        # after waiting for a model. A restart still reconciles SUBMITTING first.
        if not checkpoint(plan):
            return ApplicationOutcome(stopped=True)
        filled = await adapter.fill_application(page, plan)
        pending = unresolved_application_questions(form, filled)
        current = await adapter.read_application(page)
        if _signature(current) != signature:
            # Conditional controls can appear after selecting an answer.
            continue
        if pending:
            reasons = [f"{field.label}: {plan.unanswered_fields[field.id]}" for field in form.fields
                       if field.required and field.id in plan.unanswered_fields]
            return ApplicationOutcome(pending=reasons or pending)
        if not checkpoint(plan):
            return ApplicationOutcome(stopped=True)
        submitted_forms.add(signature)
        submission = await adapter.submit_application(page)
        if submission.status != "needs_input":
            return ApplicationOutcome(submission=submission)
    return ApplicationOutcome(pending=["Форма содержит слишком много переходов; требуется проверка пользователя"])
