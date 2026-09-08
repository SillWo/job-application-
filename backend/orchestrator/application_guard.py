from __future__ import annotations

from backend.adapters.base.protocol import ApplicationForm, FillResult


def unresolved_application_questions(
    form: ApplicationForm, fill_result: FillResult
) -> list[str]:
    """Return deduplicated questions that must stop automatic submission."""
    questions: list[str] = []
    # A positive fill result alone is not proof that an employer question was answered.
    pending = [field.label for field in form.fields if field.required and field.id not in fill_result.answered_fields]
    described = {field.label for field in form.fields}
    legacy = [question for question in form.questions if question not in described]
    for question in [*legacy, *pending, *fill_result.unknown_questions]:
        normalized = question.strip()
        if normalized and normalized not in questions:
            questions.append(normalized)
    if not fill_result.success and not questions:
        questions.append("Не удалось подтвердить заполнение формы")
    return questions
