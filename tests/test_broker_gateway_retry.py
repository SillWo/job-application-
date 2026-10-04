from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from backend.intelligence.broker_gateway import BrokeredModelGateway, _digest
from backend.intelligence.gateway import ModelPermanentError, ModelUnavailable
from backend.intelligence.hirehi_category import JobSummary
from backend.intelligence.model_broker import ModelRequestClient, SubmitRequest
from backend.intelligence.security import sanitize_untrusted_input
from backend.persistence.database import Base
from backend.persistence.execution_models import SessionExecution  # noqa: F401
from backend.persistence.model_request_models import ModelRequest
from backend.persistence.models import JobSession
from backend.persistence.pipeline_models import PipelineItem, PipelineModelOperation


def _sessions(tmp_path, name="broker-retry.db"):
    from sqlalchemy import create_engine

    engine = create_engine(
        f"sqlite:///{tmp_path / name}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _session(sessions, *, generation=3, status="RUNNING", pipeline_item=False):
    with sessions() as db:
        item = JobSession(adapter_id="hh", status=status, counters={})
        db.add(item)
        db.flush()
        db.add(SessionExecution(
            session_id=item.id,
            stage="RUNNING",
            generation=generation,
            cancel_requested=False,
        ))
        if pipeline_item:
            db.add(PipelineItem(
                session_id=item.id,
                site_id="hh",
                external_id="vacancy-1",
                stage="discovery",
                status="running",
                generation=generation,
            ))
        db.commit()
        return item.id


class ScriptedClient(ModelRequestClient):
    """A deterministic durable queue stub that stores each request normally."""

    def __init__(self, sessions, outcomes):
        super().__init__(sessions)
        self.sessions = sessions
        self.outcomes = list(outcomes)
        self.submits = 0
        self.polls: dict[str, int] = {}

    def submit_in_transaction(self, db, request: SubmitRequest):
        receipt = super().submit_in_transaction(db, request)
        self.submits += 1
        outcome = self.outcomes.pop(0) if self.outcomes else "queued"
        row = db.get(ModelRequest, receipt.request_id)
        now = datetime.now(timezone.utc)
        if outcome in {"failed", "completed", "cancelled"}:
            row.status = outcome
            row.completed_at = now
            row.error_code = (
                "deadline_exceeded" if outcome == "failed"
                else "cancelled" if outcome == "cancelled"
                else None
            )
            if outcome == "completed":
                row.canonical_output = json.dumps({"summary": "provider recovered"})
        return receipt

    def poll(self, request_id: str):
        self.polls[request_id] = self.polls.get(request_id, 0) + 1
        with self.sessions() as db:
            row = db.get(ModelRequest, request_id)
            if row.status in {"queued", "retry"} and self.polls[request_id] >= 3:
                row.status = "completed"
                row.canonical_output = json.dumps({"summary": "provider recovered"})
                row.completed_at = datetime.now(timezone.utc)
                db.commit()
        return super().poll(request_id)


def _gateway(session_id, sessions, client, *, generation=3):
    gateway = BrokeredModelGateway(
        session_id,
        "hh",
        generation,
        sessions,
        client=client,
        poll_interval=0.002,
    )
    gateway.set_context(vacancy_id="vacancy-1", stage="discovery")
    return gateway


async def _call(gateway):
    return await gateway.structured(
        "job_summary",
        {"job": {"description": "synthetic vacancy"}},
        JobSummary,
    )


@pytest.mark.asyncio
async def test_failed_request_gets_fresh_bounded_attempt_on_same_logical_operation(tmp_path):
    sessions = _sessions(tmp_path)
    session_id = _session(sessions, generation=3, pipeline_item=True)
    client = ScriptedClient(sessions, ["failed", "completed"])
    gateway = _gateway(session_id, sessions, client)

    with pytest.raises(ModelUnavailable):
        await _call(gateway)
    result = await _call(_gateway(session_id, sessions, client))

    assert result.summary == "provider recovered"
    assert client.submits == 2
    with sessions() as db:
        assert db.scalar(select(func.count(ModelRequest.id))) == 2
        assert db.scalar(select(func.count(PipelineModelOperation.id))) == 1
        failed_id = db.scalar(select(ModelRequest.id).where(ModelRequest.status == "failed"))
        operation = db.scalar(select(PipelineModelOperation))
        pipeline_item = db.scalar(select(PipelineItem))
        request = db.get(ModelRequest, operation.request_id)
        assert operation.status == "completed"
        assert request.status == "completed"
        assert operation.request_id == pipeline_item.request_id == request.id
    gateway._finish_operation(operation.id, failed_id, "failed")
    with sessions() as db:
        operation = db.get(PipelineModelOperation, operation.id)
        assert operation.status == "completed"
        assert operation.request_id == request.id


@pytest.mark.asyncio
async def test_recreated_request_cannot_reset_exhausted_logical_attempt_budget(tmp_path):
    sessions = _sessions(tmp_path)
    session_id = _session(sessions, generation=3)

    class ExhaustedClient(ScriptedClient):
        def submit_in_transaction(self, db, request):
            receipt = super().submit_in_transaction(db, request)
            row = db.get(ModelRequest, receipt.request_id)
            if row.error_code == "attempt_budget_exhausted":
                return receipt
            row.attempt = 4
            row.max_attempts = 4
            row.status = "failed"
            row.error_code = "provider_unavailable"
            row.completed_at = datetime.now(timezone.utc)
            return receipt

    client = ExhaustedClient(sessions, ["failed"])
    gateway = _gateway(session_id, sessions, client)
    with pytest.raises(ModelUnavailable):
        await _call(gateway)
    with sessions() as db:
        db.get(SessionExecution, session_id).generation = 4
        db.commit()
    with pytest.raises(ModelPermanentError, match="terminal failure"):
        await _call(_gateway(session_id, sessions, client, generation=4))
    assert client.submits == 2
    with sessions() as db:
        requests = list(db.scalars(select(ModelRequest).order_by(ModelRequest.created_at)))
        assert [row.error_code for row in requests] == ["provider_unavailable", "attempt_budget_exhausted"]


@pytest.mark.asyncio
async def test_exhausted_provider_failure_is_terminal_on_recovery_without_submit(tmp_path):
    sessions = _sessions(tmp_path)
    session_id = _session(sessions, generation=3)

    class ExhaustedClient(ScriptedClient):
        def submit_in_transaction(self, db, request):
            receipt = super().submit_in_transaction(db, request)
            row = db.get(ModelRequest, receipt.request_id)
            row.attempt = row.max_attempts
            row.status = "failed"
            row.error_code = "provider_unavailable"
            row.completed_at = datetime.now(timezone.utc)
            return receipt

    client = ExhaustedClient(sessions, ["failed"])
    gateway = _gateway(session_id, sessions, client)
    with pytest.raises(ModelUnavailable):
        await _call(gateway)

    with pytest.raises(ModelPermanentError, match="exhausted its retry budget"):
        await _call(gateway)

    assert client.submits == 1
    with sessions() as db:
        request = db.scalar(select(ModelRequest))
        assert request.error_code == "provider_unavailable"
        assert request.attempt == request.max_attempts


@pytest.mark.asyncio
async def test_unclaimed_deadline_rows_consume_budget_across_runtime_generations(tmp_path):
    sessions = _sessions(tmp_path)
    session_id = _session(sessions, generation=3)

    class ExpiredBeforeClaimClient(ScriptedClient):
        def submit_in_transaction(self, db, request):
            receipt = super().submit_in_transaction(db, request)
            row = db.get(ModelRequest, receipt.request_id)
            if row.error_code != "attempt_budget_exhausted":
                row.attempt = 0
                row.status = "failed"
                row.error_code = "deadline_exceeded"
                row.completed_at = datetime.now(timezone.utc)
            return receipt

    client = ExpiredBeforeClaimClient(sessions, ["queued"] * 5)
    for generation in range(3, 7):
        with sessions() as db:
            db.get(SessionExecution, session_id).generation = generation
            db.commit()
        with pytest.raises(ModelUnavailable):
            await _call(_gateway(session_id, sessions, client, generation=generation))

    with sessions() as db:
        db.get(SessionExecution, session_id).generation = 7
        db.commit()
    with pytest.raises(ModelPermanentError, match="terminal failure"):
        await _call(_gateway(session_id, sessions, client, generation=7))

    assert client.submits == 5
    with sessions() as db:
        requests = list(db.scalars(select(ModelRequest).order_by(ModelRequest.created_at)))
        assert [row.error_code for row in requests] == [
            "deadline_exceeded",
            "deadline_exceeded",
            "deadline_exceeded",
            "deadline_exceeded",
            "attempt_budget_exhausted",
        ]
        assert all(row.attempt == 0 for row in requests)


@pytest.mark.asyncio
async def test_broker_preserves_prompt_injection_as_typed_terminal_error(tmp_path):
    from backend.intelligence.security import PromptInjectionDetected

    sessions = _sessions(tmp_path)
    session_id = _session(sessions, generation=3)

    class UnsafeClient(ScriptedClient):
        def submit_in_transaction(self, db, request):
            receipt = super().submit_in_transaction(db, request)
            row = db.get(ModelRequest, receipt.request_id)
            row.attempt = 1
            row.status = "failed"
            row.error_code = "prompt_injection_private_data_in_output"
            row.completed_at = datetime.now(timezone.utc)
            return receipt

    client = UnsafeClient(sessions, ["failed"])
    with pytest.raises(PromptInjectionDetected) as raised:
        await _call(_gateway(session_id, sessions, client))
    assert raised.value.reason_code == "private_data_in_output"
    assert client.submits == 1


@pytest.mark.asyncio
async def test_fresh_generation_has_own_durable_identity_and_reuses_success(tmp_path):
    sessions = _sessions(tmp_path)
    session_id = _session(sessions, generation=3)
    client = ScriptedClient(sessions, ["completed"])
    payload = {"job": {"description": "synthetic vacancy"}, "requirements": "Write a summary."}

    result = await _gateway(session_id, sessions, client).fresh_generation(
        "job_summary", payload, JobSummary,
        correction_category="requirements", generation=1,
    )
    recovered = await _gateway(session_id, sessions, client).fresh_generation(
        "job_summary", payload, JobSummary,
        correction_category="requirements", generation=1,
    )

    assert result == recovered
    assert client.submits == 1
    with sessions() as db:
        request = db.scalar(select(ModelRequest))
        stored_payload = json.loads(request.canonical_input)
        assert stored_payload["generation_context"] == {
            "correction_category": "requirements",
            "correction_generation": 1,
        }
        assert db.scalar(select(func.count(PipelineModelOperation.id))) == 1


@pytest.mark.asyncio
async def test_inflight_and_completed_operation_reuse_one_request(tmp_path):
    sessions = _sessions(tmp_path)
    session_id = _session(sessions)
    client = ScriptedClient(sessions, ["queued"])
    first = asyncio.create_task(_call(_gateway(session_id, sessions, client)))
    while client.submits == 0:
        await asyncio.sleep(0.002)
    second = asyncio.create_task(_call(_gateway(session_id, sessions, client)))
    results = await asyncio.gather(first, second)
    await _call(_gateway(session_id, sessions, client))

    assert [result.summary for result in results] == ["provider recovered"] * 2
    assert client.submits == 1
    with sessions() as db:
        assert db.scalar(select(func.count(ModelRequest.id))) == 1
        assert db.scalar(select(func.count(PipelineModelOperation.id))) == 1


@pytest.mark.asyncio
async def test_concurrent_retries_claim_exactly_one_fresh_request(tmp_path):
    sessions = _sessions(tmp_path)
    session_id = _session(sessions)
    client = ScriptedClient(sessions, ["failed", "queued"])
    with pytest.raises(ModelUnavailable):
        await _call(_gateway(session_id, sessions, client))

    results = await asyncio.gather(
        _call(_gateway(session_id, sessions, client)),
        _call(_gateway(session_id, sessions, client)),
    )
    assert [result.summary for result in results] == ["provider recovered"] * 2
    assert client.submits == 2
    with sessions() as db:
        assert db.scalar(select(func.count(ModelRequest.id))) == 2
        assert db.scalar(select(func.count(PipelineModelOperation.id))) == 1


@pytest.mark.asyncio
async def test_unlinked_completed_request_is_adopted_after_submitter_crash(tmp_path, monkeypatch):
    sessions = _sessions(tmp_path)
    session_id = _session(sessions)
    client = ScriptedClient(sessions, ["completed"])
    first_gateway = _gateway(session_id, sessions, client)
    monkeypatch.setattr(
        first_gateway,
        "_submit_and_link",
        lambda _operation_id, _token, request: (
            client.submit(request)
            and (_ for _ in ()).throw(RuntimeError("simulated worker crash"))
        ),
    )
    with pytest.raises(RuntimeError, match="simulated worker crash"):
        await _call(first_gateway)

    result = await _call(_gateway(session_id, sessions, client))
    assert result.summary == "provider recovered"
    assert client.submits == 1
    with sessions() as db:
        operation = db.scalar(select(PipelineModelOperation))
        request = db.scalar(select(ModelRequest))
        assert operation.request_id == request.id
        assert operation.status == "completed"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["stopped", "generation_changed"])
async def test_failed_request_is_not_retried_after_stop_or_generation_change(tmp_path, mode):
    sessions = _sessions(tmp_path)
    session_id = _session(sessions)
    client = ScriptedClient(sessions, ["failed"])
    with pytest.raises(ModelUnavailable):
        await _call(_gateway(session_id, sessions, client))
    with sessions() as db:
        item = db.get(JobSession, session_id)
        execution = db.scalar(select(SessionExecution).where(SessionExecution.session_id == session_id))
        if mode == "stopped":
            item.status = "STOPPING"
        else:
            execution.generation = 4
        db.commit()

    with pytest.raises(asyncio.CancelledError):
        await _call(_gateway(session_id, sessions, client))
    assert client.submits == 1
    with sessions() as db:
        assert db.scalar(select(func.count(ModelRequest.id))) == 1


@pytest.mark.asyncio
async def test_cancelled_request_in_live_generation_is_explicitly_not_retried(tmp_path):
    sessions = _sessions(tmp_path)
    session_id = _session(sessions)
    client = ScriptedClient(sessions, ["cancelled"])
    with pytest.raises(asyncio.CancelledError):
        await _call(_gateway(session_id, sessions, client))
    with pytest.raises(asyncio.CancelledError):
        await _call(_gateway(session_id, sessions, client))
    assert client.submits == 1
    with sessions() as db:
        assert db.scalar(select(func.count(ModelRequest.id))) == 1
        assert db.scalar(select(PipelineModelOperation.status)) == "cancelled"


@pytest.mark.asyncio
async def test_expired_reservation_fences_delayed_submitter_without_duplicate_request(tmp_path):
    sessions = _sessions(tmp_path)
    session_id = _session(sessions, pipeline_item=True)
    client = ScriptedClient(sessions, ["failed", "completed"])
    gateway = _gateway(session_id, sessions, client)
    with pytest.raises(ModelUnavailable):
        await _call(gateway)

    payload = sanitize_untrusted_input(
        {"job": {"description": "synthetic vacancy"}}, context="job_summary.input"
    )
    versions = gateway._versions("job_summary", payload, JobSummary)
    input_hash = _digest(payload)
    versions_hash = _digest(asdict(versions))
    operation, _no_claim = gateway._operation(
        "job_summary", "discovery", input_hash, versions_hash
    )
    _, old_token = gateway._recover_request_id(
        operation, "job_summary", "discovery", input_hash, versions
    )
    assert old_token
    with sessions() as db:
        current = db.get(PipelineModelOperation, operation.id)
        item = db.scalar(select(PipelineItem))
        assert item.request_id is None
        assert item.diagnostic_id is None
        current.updated_at = datetime.now(timezone.utc) - timedelta(seconds=60)
        db.commit()

    new_gateway = _gateway(session_id, sessions, client)
    _, new_token = new_gateway._recover_request_id(
        operation, "job_summary", "discovery", input_hash, versions
    )
    assert new_token and new_token != old_token
    request = SubmitRequest(
        session_id=session_id,
        site_id="hh",
        vacancy_id="vacancy-1",
        stage="discovery",
        role="job_summary",
        payload=payload,
        schema=JobSummary,
        versions=versions,
        generation=3,
    )
    assert gateway._submit_and_link(operation.id, old_token, request) is None
    fresh_id = new_gateway._submit_and_link(operation.id, new_token, request)
    assert fresh_id is not None
    assert client.submits == 2
    new_gateway._finish_operation(operation.id, fresh_id, "completed")
    with sessions() as db:
        assert db.scalar(select(func.count(ModelRequest.id))) == 2
        current = db.get(PipelineModelOperation, operation.id)
        assert current.request_id == fresh_id
        assert current.status == "completed"


@pytest.mark.asyncio
async def test_claim_lost_after_initial_read_cannot_insert_request(tmp_path, monkeypatch):
    sessions = _sessions(tmp_path)
    session_id = _session(sessions)
    client = ScriptedClient(sessions, ["queued"])
    gateway = _gateway(session_id, sessions, client)
    payload = sanitize_untrusted_input(
        {"job": {"description": "synthetic vacancy"}}, context="job_summary.input"
    )
    versions = gateway._versions("job_summary", payload, JobSummary)
    operation, old_token = gateway._operation(
        "job_summary", "discovery", _digest(payload), _digest(asdict(versions))
    )
    request = SubmitRequest(
        session_id=session_id,
        site_id="hh",
        vacancy_id="vacancy-1",
        stage="discovery",
        role="job_summary",
        payload=payload,
        schema=JobSummary,
        versions=versions,
        generation=3,
    )
    original_confirm = gateway._confirm_submission_claim

    def steal_reservation(db, operation_id, token):
        with sessions() as competing_db:
            competing = competing_db.get(PipelineModelOperation, operation_id)
            competing.request_id = "replacement-reservation"
            competing.updated_at = datetime.now(timezone.utc)
            competing_db.commit()
        return original_confirm(db, operation_id, token)

    monkeypatch.setattr(gateway, "_confirm_submission_claim", steal_reservation)
    assert gateway._submit_and_link(operation.id, old_token, request) is None
    assert client.submits == 0
    with sessions() as db:
        assert db.scalar(select(func.count(ModelRequest.id))) == 0
        competing = db.get(PipelineModelOperation, operation.id)
        assert competing.request_id == "replacement-reservation"
