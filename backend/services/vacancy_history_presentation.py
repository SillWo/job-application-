"""Read-only resolution of cross-session vacancy history for presentation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from sqlalchemy.orm import Session

from backend.persistence.models import Application, Vacancy

HistoryOutcome = Literal["confirmed", "already_applied", "partial", "unconfirmed"]


@dataclass(frozen=True)
class HistoryResolution:
    origin: Vacancy | None
    outcome: HistoryOutcome
    invalid_history: bool
    used_alias: bool


def _data(row: Vacancy) -> dict:
    return row.data if isinstance(row.data, dict) else {}


def _has_application(db: Session, row: Vacancy) -> bool:
    return db.query(Application.id).filter(
        Application.vacancy_id == row.id,
        Application.status.in_(("submitted", "already_applied")),
    ).first() is not None


def _own_outcome(db: Session, row: Vacancy) -> HistoryOutcome | None:
    data = _data(row)
    progress = data.get("submission_progress")
    progress = progress if isinstance(progress, dict) else {}
    if _has_application(db, row) or row.state in {"SUBMITTED", "REPORTED"}:
        return "confirmed"
    if progress.get("cv_confirmed") and progress.get("cover_letter_confirmed"):
        return "confirmed"
    if progress.get("cv_confirmed") and progress.get("cover_letter_pending"):
        return "partial"
    if row.state == "ALREADY_APPLIED" and not data.get("cross_session_suppressed"):
        return "already_applied"
    if row.state == "PARTIAL" or progress.get("cv_confirmed") or progress.get("cover_letter_confirmed"):
        return "partial"
    code = row.error_code or data.get("error_code")
    if row.state in {"SUBMISSION_UNCONFIRMED", "UNCONFIRMED"} or code == "SUBMISSION_UNCONFIRMED":
        return "unconfirmed"
    if (
        row.state == "SUBMITTING"
        or data.get("submission_attempted")
        or data.get("recovery_unresolved")
        or data.get("partial_recovery_blocked")
        or data.get("submission_reconciliation_attempts")
    ):
        return "unconfirmed"
    return None


def resolve_external_history(
    db: Session, vacancy: Vacancy, max_depth: int = 32
) -> HistoryResolution:
    """Resolve a suppressed mirror to a validated, older source row.

    The walk is deliberately bounded and fail-closed. It only follows integer
    primary keys for the same source and external id. No row is modified.
    """
    root_data = _data(vacancy)
    if not root_data.get("cross_session_suppressed"):
        outcome = _own_outcome(db, vacancy) or "unconfirmed"
        return HistoryResolution(vacancy, outcome, False, False)

    current = vacancy
    visited = {vacancy.id}
    limit = max(0, int(max_depth))
    for _ in range(limit):
        # Fresh evidence on any row in the chain takes precedence over older
        # pointers. ALREADY_APPLIED alone is excluded for suppressed rows.
        own = _own_outcome(db, current)
        if own in {"confirmed", "partial"}:
            return HistoryResolution(current, own, False, current.id != vacancy.id)
        data = _data(current)
        raw_id = data.get("historical_vacancy_id")
        if raw_id is None:
            raw_id = data.get("historical_source_vacancy_id")
        if isinstance(raw_id, bool) or not isinstance(raw_id, int) or raw_id <= 0:
            return HistoryResolution(None, "unconfirmed", True, True)
        if raw_id in visited:
            return HistoryResolution(None, "unconfirmed", True, True)
        target = db.get(Vacancy, raw_id)
        if target is None or target.id >= current.id or target.source != vacancy.source or target.external_id != vacancy.external_id:
            return HistoryResolution(None, "unconfirmed", True, True)
        visited.add(target.id)
        current = target
        if not _data(current).get("cross_session_suppressed"):
            own = _own_outcome(db, current)
            if own is not None:
                return HistoryResolution(current, own, False, True)
            # A leaf with no evidence of an attempted or confirmed submission
            # does not become success merely because the mirror said so.
            return HistoryResolution(current, "unconfirmed", False, True)
    # If another pointer exists after the allowed steps, the history is too
    # deep to validate safely.
    data = _data(current)
    if data.get("historical_vacancy_id") is not None or data.get("historical_source_vacancy_id") is not None:
        return HistoryResolution(None, "unconfirmed", True, True)
    own = _own_outcome(db, current)
    return HistoryResolution(current, own or "unconfirmed", False, True)
