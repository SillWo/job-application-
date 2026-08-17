from __future__ import annotations

from backend.adapters.base.protocol import ApplicationForm, FillResult


def unresolved_application_questions(
    form: ApplicationForm, fill_result: FillResult
) -> list[str]:
    """Return deduplicated questions that must stop automatic submission."""
    questions: list[str] = []
    for question in [*form.questions, *fill_result.unknown_questions]:
        normalized = question.strip()
        if normalized and normalized not in questions:
            questions.append(normalized)
    return questions
