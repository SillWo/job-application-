from backend.schemas.domain import VacancyState

TRANSITIONS = {
    VacancyState.DISCOVERED: {VacancyState.EXTRACTED, VacancyState.ALREADY_APPLIED, VacancyState.FAILED},
    VacancyState.EXTRACTED: {VacancyState.FILTERED_OUT, VacancyState.EVALUATING, VacancyState.SKIPPED_TEST},
    VacancyState.EVALUATING: {VacancyState.REJECTED_BY_MODEL, VacancyState.NEEDS_REVIEW, VacancyState.LETTER_GENERATED, VacancyState.READY_TO_SUBMIT},
    VacancyState.LETTER_GENERATED: {VacancyState.READY_TO_SUBMIT},
    VacancyState.READY_TO_SUBMIT: {VacancyState.NEEDS_REVIEW, VacancyState.FILLING_FORM},
    VacancyState.FILLING_FORM: {VacancyState.NEEDS_REVIEW, VacancyState.SUBMITTING, VacancyState.FAILED},
    VacancyState.SUBMITTING: {VacancyState.SUBMITTED, VacancyState.UNKNOWN_RESULT, VacancyState.FAILED},
}


def ensure_transition(current: str, target: str) -> None:
    source, destination = VacancyState(current), VacancyState(target)
    if destination not in TRANSITIONS.get(source, set()):
        raise ValueError(f"Недопустимый переход {source} -> {destination}")

