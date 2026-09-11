from __future__ import annotations

from backend.adapters.base.protocol import ApplicationForm, FillResult
from backend.intelligence.security import sanitize_untrusted_input


def unresolved_application_questions(
    form: ApplicationForm, fill_result: FillResult
) -> list[str]:
    """Return deduplicated questions that must stop automatic submission."""
    questions: list[str] = []

    def clean_question(value: str) -> str:
        cleaned = sanitize_untrusted_input(value, context="application question")
        return str(cleaned or "").strip()

    # A positive fill result alone is not proof that an employer question was answered.
    pending = []
    for field in form.fields:
        if field.required and field.id not in fill_result.answered_fields:
            # Keep a generic reason for an instruction-only label: the field is
            # still required by the live form even when its semantic text was
            # removed before reaching model or user-facing paths.
            pending.append(clean_question(field.label) or "Обязательное поле формы осталось без ответа")
    described = {clean_question(field.label) for field in form.fields}
    legacy = [clean_question(question) for question in form.questions]
    legacy = [question for question in legacy if question and question not in described]
    unknown = [clean_question(question) for question in fill_result.unknown_questions]
    for question in [*legacy, *pending, *unknown]:
        normalized = question.strip()
        if normalized and normalized not in questions:
            questions.append(normalized)
    if not fill_result.success and not questions:
        questions.append("Не удалось подтвердить заполнение формы")
    return questions
