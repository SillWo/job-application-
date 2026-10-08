from __future__ import annotations

import asyncio
import json
import threading
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from backend.persistence.database import Base
from backend.persistence.model_request_models import ModelRequest, ModelResponseCache
from backend.persistence.models import Application, JobSession, Vacancy
from backend.persistence.pipeline_models import PipelineCheckpoint, PipelineModelOperation
from backend.services.payload_retention import prune_payloads, run_retention_maintenance


def _factory(tmp_path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'retention.db').as_posix()}")
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine, expire_on_commit=False)


def _request(session_id: int, *, status="completed", error_code=None, marker="row") -> ModelRequest:
    created = datetime(2026, 1, 1, tzinfo=timezone.utc)
    row = ModelRequest(
        id=f"request-{session_id}-{marker}",
        diagnostic_id=f"diagnostic-{session_id}-{marker}",
        session_id=session_id,
        site_id="hh",
        vacancy_id=f"vacancy-{marker}",
        stage="evaluation",
        role="job_summary",
        schema_ref="backend.intelligence.hirehi_category:JobSummary",
        status=status,
        generation=1,
        attempt=1,
        max_attempts=4,
        model_id="test/model",
        model_version="v1",
        prompt_version="p1",
        schema_version="s1",
        parser_version="r1",
        input_hash="i" * 64,
        cache_key=(marker + str(session_id)).ljust(64, "x")[:64],
        canonical_input=json.dumps({"resume": "candidate payload " * 100}),
        canonical_output='{"summary":"candidate response"}' if status == "completed" else None,
        created_at=created,
        available_at=created,
        deadline_at=created + timedelta(hours=2),
        completed_at=created + timedelta(hours=1),
        error_code=error_code,
    )
    return row


def _terminal(item_id: int, *, status="COMPLETED") -> JobSession:
    old = datetime(2026, 1, 2, tzinfo=timezone.utc)
    return JobSession(
        id=item_id,
        adapter_id="hh",
        status=status,
        counters={},
        started_at=old - timedelta(days=1),
        finished_at=old,
        recovery={},
    )


def test_payload_retention_clears_only_old_safe_terminal_sessions(tmp_path):
    engine, factory = _factory(tmp_path)
    old_now = datetime(2026, 3, 1, tzinfo=timezone.utc)
    with factory() as db:
        eligible = _terminal(1)
        db.add_all([
            eligible,
            _terminal(2, status="RUNNING"),
            _terminal(3, status="PAUSED"),
            _terminal(4),
            _terminal(5),
            _terminal(6),
            _terminal(7),
            _terminal(8),
            _terminal(9),
            _terminal(10),
            _terminal(11),
                _terminal(12),
        ])
        safe_completed = _request(1, marker="completed")
        safe_failed = _request(1, status="failed", error_code="provider_unavailable", marker="failed")
        safe_cancelled = _request(1, status="cancelled", error_code="cancelled", marker="cancelled")
        db.add_all([safe_completed, safe_failed, safe_cancelled])
        db.flush()
        db.add(ModelResponseCache(
            session_id=1,
            cache_key=safe_completed.cache_key,
            source_request_id=safe_completed.id,
            canonical_output=safe_completed.canonical_output,
            created_at=safe_completed.created_at,
        ))

        active_request = _request(2, status="queued", marker="queued")
        paused_request = _request(3, status="retry", marker="retry")
        db.add_all([active_request, paused_request])
        for sid, state, code, data in [
            (4, "PARTIAL", None, {"submission_progress": {"cv_confirmed": True, "cover_letter_pending": True}}),
            (5, "SUBMITTING", None, {"submission_reconciliation_attempts": 1}),
            (6, "ERROR", "SUBMISSION_UNCONFIRMED", {}),
        ]:
            vacancy = Vacancy(
                session_id=sid,
                source="hh",
                site="HH.ru",
                external_id=f"external-{sid}",
                url=f"https://example.test/{sid}",
                title="Retained vacancy",
                state=state,
                error_code=code,
                data=data,
            )
            db.add(vacancy)
        db.add(PipelineCheckpoint(
            session_id=7,
            site_id="hh",
            name="submission-checkpoint",
            generation=1,
            revision=1,
            data={"status": "unknown"},
            updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        ))
        operation_request = _request(8, marker="operation")
        db.add(operation_request)
        db.add(PipelineModelOperation(
            session_id=8,
            site_id="hh",
            vacancy_key="operation",
            stage="evaluation",
            role="job_summary",
            input_hash="a" * 64,
            versions_hash="b" * 64,
            generation=1,
            request_id=operation_request.id,
            diagnostic_id=operation_request.diagnostic_id,
            status="running",
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        ))
        db.add(Application(
            vacancy_id=9,
            status="unknown",
            submitted_at=None,
        ))
        # Existing Application rows need a real vacancy foreign key.
        db.add(Vacancy(
            id=9,
            session_id=9,
            source="hh",
            site="HH.ru",
            external_id="app-9",
            url="https://example.test/app-9",
            title="Uncertain application",
            state="ERROR",
            data={},
        ))
        db.add(_request(10, marker="active-link"))
        db.add(PipelineModelOperation(
            session_id=10,
            site_id="hh",
            vacancy_key="active-link",
            stage="submission",
            role="application_answers",
            input_hash="c" * 64,
            versions_hash="d" * 64,
            generation=1,
            request_id="unlinked-token",
            diagnostic_id=None,
            status="submitting",
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        ))
        recovery_guard = db.get(JobSession, 11)
        recovery_guard.recovery = {"submission_checkpoint": {"status": "pending"}}
        generic_recovery_guard = db.get(JobSession, 12)
        generic_recovery_guard.recovery = {"status": "recovering"}
        db.commit()

        result = prune_payloads(db, now=old_now)
        assert result == {
            "sessions_checked": 10,
            "cache_rows_deleted": 1,
            "requests_cleared": 3,
            "next_session_id": 12,
        }
        db.expire_all()

        completed = db.get(ModelRequest, safe_completed.id)
        assert completed.canonical_input == completed.canonical_output == "{}"
        assert completed.error_code == "payload_retained_metadata"
        failed = db.get(ModelRequest, safe_failed.id)
        assert failed.canonical_input == "{}"
        assert failed.canonical_output is None
        assert failed.error_code == "provider_unavailable"
        cancelled = db.get(ModelRequest, safe_cancelled.id)
        assert cancelled.canonical_input == "{}"
        assert cancelled.canonical_output is None
        assert cancelled.error_code == "cancelled"
        assert db.scalar(select(ModelResponseCache.id)) is None
        for request in (active_request, paused_request, operation_request):
            assert db.get(ModelRequest, request.id).canonical_input != "{}"

        second = prune_payloads(db, now=old_now)
        assert second["cache_rows_deleted"] == 0
        assert second["requests_cleared"] == 0
        db.commit()
    engine.dispose()


def test_legacy_partial_progress_preserves_model_payload_and_cache(tmp_path):
    engine, factory = _factory(tmp_path)
    old_now = datetime(2026, 3, 1, tzinfo=timezone.utc)
    with factory() as db:
        db.add(_terminal(1))
        request = _request(1, marker="legacy-partial")
        db.add(request)
        db.flush()
        db.add(ModelResponseCache(
            session_id=1,
            cache_key=request.cache_key,
            source_request_id=request.id,
            canonical_output=request.canonical_output,
            created_at=request.created_at,
        ))
        db.add(Vacancy(
            session_id=1,
            source="hh",
            site="HH.ru",
            external_id="legacy-partial",
            url="https://example.test/legacy-partial",
            title="Legacy partial submission",
            state="ERROR",
            data={"submission_progress": {"cv_confirmed": True, "cover_letter_pending": True}},
        ))
        db.commit()

        result = prune_payloads(db, now=old_now)

        assert result["requests_cleared"] == 0
        assert result["cache_rows_deleted"] == 0
        assert db.get(ModelRequest, request.id).canonical_input != "{}"
        assert db.scalar(select(ModelResponseCache.id)) is not None
    engine.dispose()


def test_retention_cursor_advances_past_large_protected_prefix(tmp_path):
    engine, factory = _factory(tmp_path)
    old_now = datetime(2026, 3, 1, tzinfo=timezone.utc)
    with factory() as db:
        protected = [_terminal(index) for index in range(1, 1001)]
        for item in protected:
            item.recovery = {"submission_checkpoint": {"status": "pending"}}
        db.add_all(protected)
        db.add(_terminal(1001))
        db.add(_request(1001, marker="later-safe"))
        db.commit()

        first = prune_payloads(db, now=old_now)
        assert first["sessions_checked"] == 1000
        assert first["requests_cleared"] == 0
        assert first["next_session_id"] == 1000

        second = prune_payloads(db, now=old_now, after_session_id=first["next_session_id"])
        assert second["sessions_checked"] == 1
        assert second["requests_cleared"] == 1
        assert second["next_session_id"] == 1001
        assert db.get(ModelRequest, "request-1001-later-safe").error_code == "payload_retained_metadata"
    engine.dispose()


@pytest.mark.asyncio
async def test_retention_maintenance_stops_after_its_bounded_pass(tmp_path, monkeypatch):
    engine, factory = _factory(tmp_path)
    pass_finished = threading.Event()

    class ObservedSession:
        def __enter__(self):
            self.db = factory()
            return self.db

        def __exit__(self, exc_type, exc, traceback):
            try:
                return self.db.__exit__(exc_type, exc, traceback)
            finally:
                pass_finished.set()

    class ObservedFactory:
        def __call__(self):
            return ObservedSession()

    from backend.persistence import database

    monkeypatch.setattr(database, "SessionLocal", ObservedFactory())
    stop = asyncio.Event()
    task = asyncio.create_task(run_retention_maintenance(stop, interval_seconds=60))
    assert await asyncio.to_thread(pass_finished.wait, 3)
    stop.set()
    await asyncio.wait_for(task, timeout=1)
    engine.dispose()
