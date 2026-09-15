"""Bounded HH response steps; persistence and cancellation stay with the workflow."""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from backend.adapters.base.protocol import SubmissionResult
from backend.intelligence.application_answers import prepare_answers
from backend.intelligence.security import (
    assert_safe_outgoing_text,
    sanitize_untrusted_input,
)
from backend.orchestrator.application_guard import unresolved_application_questions
from backend.services.resume_session import render_local_private


@dataclass
class ApplicationOutcome:
    submission: SubmissionResult | None = None
    error_code: str | None = None
    error_message: str | None = None
    unanswered_questions: list[str] = field(default_factory=list)
    stopped: bool = False


def _signature(form) -> str:
    payload = form.model_dump(include={"fields", "questions", "confirmation"})
    # Sanitizing an instruction-only legacy question leaves an empty semantic
    # placeholder. It must not make an unchanged form look like a new step.
    payload["questions"] = [question for question in payload.get("questions", []) if str(question).strip()]
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


async def complete_application(adapter, page, plan, job, profile, resumes, description,
                               gateway, checkpoint, max_steps=5, *, memory=None,
                               guaranteed_application=False, private_view=None) -> ApplicationOutcome:
    submitted_forms: set[str] = set()

    def assert_plan_outgoing(current_plan, *, context: str, source_form=None) -> None:
        if current_plan.cover_letter:
            assert_safe_outgoing_text(
                current_plan.cover_letter, profile, resumes, context=f"{context}_letter"
            )
        current_options = {
            field.id: set(field.options)
            for field in getattr(source_form, "fields", [])
        }
        for answer in current_plan.form_answers.values():
            for value in answer.values:
                # Preserve exact employer options for binding even when an
                # option's source wording happens to resemble an instruction.
                allowed_options = current_options.get(answer.field.id)
                if allowed_options is None and source_form is None:
                    allowed_options = set(answer.field.options)
                if allowed_options is not None and value in allowed_options:
                    continue
                assert_safe_outgoing_text(value, profile, resumes, context=f"{context}_answer")
        for value in current_plan.known_answers.values():
            assert_safe_outgoing_text(value, profile, resumes, context=f"{context}_known_answer")

    for _ in range(max_steps):
        # A durable plan can contain stale field metadata.  Do not trust its
        # option labels as an outbound allowlist until the live form has been
        # read for this attempt.
        assert_plan_outgoing(plan, context="application_plan")
        if not checkpoint(plan):
            return ApplicationOutcome(stopped=True)
        form = await adapter.prepare_application(page, plan)
        model_form = sanitize_untrusted_input(form, context="application_form")
        if form.confirmation:
            return ApplicationOutcome(
                error_code="FOREIGN_APPLICATION_CONFIRMATION_FAILED",
                error_message="Не удалось автоматически подтвердить отклик в другой стране",
            )
        # Dynamic-form detection compares semantic sanitized content; the
        # original form remains available for adapter binding and submission.
        signature = _signature(model_form)
        if signature in submitted_forms:
            return ApplicationOutcome(
                error_code="APPLICATION_FORM_STUCK",
                error_message="HH.ru оставил ту же форму после отправки; автоматическое заполнение остановлено",
            )
        plan = await prepare_answers(gateway, form, plan, job, profile, resumes, description,
                                     memory=memory, guaranteed_application=guaranteed_application,
                                     private_view=private_view)
        if private_view is not None:
            plan.cover_letter = render_local_private(plan.cover_letter, private_view) if plan.cover_letter else ""
            for answer in plan.form_answers.values():
                answer.values = [render_local_private(value, private_view) for value in answer.values]
            plan.known_answers = {
                key: render_local_private(value, private_view)
                for key, value in plan.known_answers.items()
            }
        # The freshly read form is the authority for fixed-choice values.  It
        # also lets legitimate employer options contain instruction-like
        # wording without treating them as free-form model output.
        assert_plan_outgoing(plan, context="application_plan", source_form=form)
        # Persist the generated answers before any field is changed, and recheck cancellation
        # after waiting for a model. A restart still reconciles SUBMITTING first.
        if not checkpoint(plan):
            return ApplicationOutcome(stopped=True)
        filled = await adapter.fill_application(page, plan)
        model_filled = sanitize_untrusted_input(filled, context="application_fill_result")
        pending = unresolved_application_questions(model_form, model_filled)
        current = await adapter.read_application(page)
        model_current = sanitize_untrusted_input(current, context="application_form_after_fill")
        if _signature(model_current) != signature:
            # Conditional controls can appear after selecting an answer.
            continue
        if pending:
            reasons = [f"{field.label}: {plan.unanswered_fields[field.id]}" for field in form.fields
                       if field.required and field.id in plan.unanswered_fields]
            unanswered = reasons or pending
            return ApplicationOutcome(
                error_code="APPLICATION_FORM_UNRESOLVED",
                error_message="Не удалось безопасно заполнить обязательные вопросы анкеты: " + "; ".join(unanswered[:3]),
                unanswered_questions=unanswered,
            )
        if not checkpoint(plan):
            return ApplicationOutcome(stopped=True)
        sanitize_untrusted_input(current, context="application_form_before_submit")
        assert_plan_outgoing(plan, context="application_plan_before_submit", source_form=current)
        submitted_forms.add(signature)
        submission = await adapter.submit_application(page)
        if submission.status != "needs_input":
            return ApplicationOutcome(submission=submission)
    return ApplicationOutcome(
        error_code="APPLICATION_FORM_STUCK",
        error_message="Форма содержит слишком много переходов для автоматической обработки",
    )
