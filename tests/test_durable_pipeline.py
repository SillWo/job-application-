from __future__ import annotations

import asyncio
import json
from collections import Counter
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from backend.adapters.base.protocol import JobRef
from backend.intelligence.broker_gateway import BrokeredModelGateway
from backend.intelligence.hirehi_category import JobSummary
from backend.intelligence.model_broker import ModelRequestBroker, ProviderCall
from backend.orchestrator.pipeline import DurablePipelineCoordinator, PipelineStore
from backend.persistence.database import Base
from backend.persistence.execution_models import SessionExecution  # noqa: F401
from backend.persistence.model_request_models import ModelRequest
from backend.persistence.models import JobSession, Vacancy
from backend.persistence.pipeline_models import (
    PipelineCheckpoint,
    PipelineItem,
    PipelineModelOperation,
)


def factory(tmp_path, name="pipeline.db"):
    engine = create_engine(
        f"sqlite:///{tmp_path / name}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def add_session(db, site: str, *, status: str = "RUNNING") -> int:
    row = JobSession(adapter_id=site, status=status, counters={})
    db.add(row)
    db.commit()
    return row.id


def refs(count: int, prefix: str = "vacancy") -> list[JobRef]:
    return [
        JobRef(external_id=f"{prefix}-{index}", url=f"https://example.test/{prefix}-{index}")
        for index in range(count)
    ]


def test_backpressure_high_water_is_exactly_ten_and_overflow_is_lossless(tmp_path):
    sessions = factory(tmp_path)
    with sessions() as db:
        session_id = add_session(db, "hh")
    pipeline = DurablePipelineCoordinator(sessions)

    first = pipeline.enqueue(session_id, "hh", refs(15), generation=0)
    assert len(first) == 10
    assert pipeline.queue_metrics("hh") == {"active": 10, "high_water": 10, "limit": 10}

    with sessions() as db:
        checkpoint = db.scalar(select(PipelineCheckpoint))
        assert len(checkpoint.data["backlog"]) == 5
        assert len(checkpoint.data["urls"]) == 15
        for item in list(db.scalars(select(PipelineItem).order_by(PipelineItem.id)))[:5]:
            vacancy = Vacancy(
                session_id=session_id,
                source="hh",
                external_id=item.external_id,
                url=f"https://example.test/{item.external_id}",
                title=item.external_id,
                state="SUBMITTED",
                data={},
            )
            db.add(vacancy)
            db.flush()
            item.vacancy_id = vacancy.id
        db.commit()

    second = pipeline.enqueue(session_id, "hh", [], generation=0)
    assert len(second) == 10
    assert {item.external_id for item in second}.issuperset(
        {f"vacancy-{index}" for index in range(10, 15)}
    )
    assert pipeline.queue_metrics("hh")["high_water"] == 10
    with sessions() as db:
        assert db.scalar(select(func.count(PipelineItem.id))) == 15
        checkpoint = db.scalar(select(PipelineCheckpoint))
        assert checkpoint.data["backlog"] == []


def test_capacity_is_global_per_site_across_sessions(tmp_path):
    sessions = factory(tmp_path)
    with sessions() as db:
        first_id = add_session(db, "hh")
        second_id = add_session(db, "hh")
    pipeline = DurablePipelineCoordinator(sessions)
    assert len(pipeline.enqueue(first_id, "hh", refs(8, "a"))) == 8
    assert len(pipeline.enqueue(second_id, "hh", refs(8, "b"))) == 2
    assert pipeline.queue_metrics("hh") == {"active": 10, "high_water": 10, "limit": 10}


def test_missing_recovery_url_is_failed_and_never_reaches_adapter(tmp_path):
    sessions = factory(tmp_path)
    with sessions() as db:
        session_id = add_session(db, "hh")
        db.add(PipelineItem(
            session_id=session_id,
            site_id="hh",
            external_id="damaged",
            stage="discovery",
            status="queued",
            generation=0,
        ))
        db.commit()
    assert DurablePipelineCoordinator(sessions).enqueue(session_id, "hh", []) == []
    with sessions() as db:
        item = db.scalar(select(PipelineItem))
        assert (item.status, item.error_code) == ("failed", "missing_source_url")


def test_generation_takeover_adopts_queue_and_backlog_without_reviving_terminal_items(tmp_path):
    sessions = factory(tmp_path)
    pipeline = DurablePipelineCoordinator(sessions)
    with sessions() as db:
        session_id = add_session(db, "hh")
        execution = SessionExecution(session_id=session_id, generation=1)
        db.add(execution)
        active_refs = refs(15)
        terminal_refs = refs(2, "terminal")
        urls = {ref.external_id: ref.url for ref in [*active_refs, *terminal_refs]}
        db.add(PipelineCheckpoint(
            session_id=session_id,
            site_id="hh",
            name="discovery",
            generation=1,
            revision=3,
            data={
                "backlog": [
                    ref.model_dump(mode="json") for ref in [*active_refs[10:], *terminal_refs]
                ],
                "high_water": 15,
                "cursor": "page-4",
                "urls": urls,
            },
        ))
        active_items = []
        for index, ref in enumerate(active_refs[:10]):
            item = PipelineItem(
                session_id=session_id,
                site_id="hh",
                external_id=ref.external_id,
                stage="evaluation" if index == 0 else "discovery",
                status="running" if index == 0 else "queued",
                generation=1,
                stage_revision=2 if index == 0 else 0,
                request_id="old-request" if index == 0 else None,
                diagnostic_id="old-diagnostic" if index == 0 else None,
            )
            db.add(item)
            active_items.append(item)
        evaluating = Vacancy(
            session_id=session_id,
            source="hh",
            external_id=active_refs[0].external_id,
            url=active_refs[0].url,
            title="Evaluating",
            state="EVALUATING",
            data={},
        )
        db.add(evaluating)
        for ref, status in zip(terminal_refs, ("completed", "cancelled"), strict=True):
            db.add(PipelineItem(
                session_id=session_id,
                site_id="hh",
                external_id=ref.external_id,
                stage="completed" if status == "completed" else "evaluation",
                status=status,
                generation=1,
            ))
        db.flush()
        active_items[0].vacancy_id = evaluating.id
        db.add(PipelineModelOperation(
            pipeline_item_id=active_items[0].id,
            session_id=session_id,
            site_id="hh",
            vacancy_key=active_refs[0].external_id,
            stage="evaluation",
            role="job_summary",
            input_hash="a" * 64,
            versions_hash="b" * 64,
            generation=1,
            request_id="old-request",
            diagnostic_id="old-diagnostic",
            status="running",
        ))
        execution.generation = 2
        assert PipelineStore.adopt_generation(db, session_id, "hh", 2)
        db.commit()

    with sessions() as db:
        adopted = list(db.scalars(select(PipelineItem).where(
            PipelineItem.session_id == session_id,
            PipelineItem.generation == 2,
        ).order_by(PipelineItem.created_at, PipelineItem.id)))
        assert len(adopted) == 10
        assert adopted[0].external_id == active_refs[0].external_id
        assert (adopted[0].stage, adopted[0].status, adopted[0].stage_revision) == (
            "evaluation", "running", 2
        )
        assert all(item.request_id is None and item.diagnostic_id is None for item in adopted)
        checkpoint = db.scalar(select(PipelineCheckpoint).where(
            PipelineCheckpoint.session_id == session_id,
            PipelineCheckpoint.generation == 2,
        ))
        assert [ref["external_id"] for ref in checkpoint.data["backlog"]] == [
            ref.external_id for ref in active_refs[10:]
        ]
        assert checkpoint.data["high_water"] == 15
        assert checkpoint.data["cursor"] == "page-4"
        assert checkpoint.data["urls"] == urls
        operations = list(db.scalars(select(PipelineModelOperation)))
        assert len(operations) == 1
        assert (operations[0].generation, operations[0].status) == (1, "cancelled")
        assert operations[0].request_id is None and operations[0].diagnostic_id is None
        terminal = list(db.scalars(select(PipelineItem).where(
            PipelineItem.external_id.in_([ref.external_id for ref in terminal_refs])
        )))
        assert {(item.generation, item.status) for item in terminal} == {
            (1, "completed"), (1, "cancelled")
        }

    # Active ten-item work keeps the site cap; the overflow is retained but
    # cannot be admitted until one of the active rows reaches a terminal state.
    assert len(pipeline.enqueue(session_id, "hh", [], generation=2)) == 10
    assert pipeline.enqueue(session_id, "hh", refs(1, "stale"), generation=1) == []
    assert pipeline.advance(
        session_id, "hh", active_refs[0].external_id, "completed", generation=1
    ) is False
    with sessions() as db:
        assert db.scalar(select(func.count(PipelineItem.id)).where(
            PipelineItem.session_id == session_id,
            PipelineItem.generation == 2,
        )) == 10
        assert db.scalar(select(PipelineItem.status).where(
            PipelineItem.session_id == session_id,
            PipelineItem.external_id == active_refs[0].external_id,
            PipelineItem.generation == 2,
        )) == "running"


def test_capacity_ignores_stale_generation_and_terminal_session_items(tmp_path):
    sessions = factory(tmp_path)
    with sessions() as db:
        stale_session_id = add_session(db, "hh")
        terminal_session_id = add_session(db, "hh", status="STOPPED")
        live_session_id = add_session(db, "hh")
        db.add_all([
            SessionExecution(session_id=stale_session_id, generation=2),
            SessionExecution(session_id=terminal_session_id, generation=0),
        ])
        for session_id, generation, prefix in (
            (stale_session_id, 1, "stale"),
            (terminal_session_id, 0, "terminal"),
        ):
            for ref in refs(10, prefix):
                db.add(PipelineItem(
                    session_id=session_id,
                    site_id="hh",
                    external_id=ref.external_id,
                    stage="evaluation",
                    status="running",
                    generation=generation,
                ))
        db.commit()
    pipeline = DurablePipelineCoordinator(sessions)
    assert len(pipeline.enqueue(live_session_id, "hh", refs(10, "live"))) == 10
    assert pipeline.queue_metrics("hh")["active"] == 10


@pytest.mark.asyncio
async def test_takeover_cancels_old_broker_requests_and_keeps_current_generation(tmp_path):
    sessions = factory(tmp_path)
    now = datetime.now(timezone.utc)
    old_running_id = "old-running-request"
    old_queued_id = "old-queued-request"
    current_id = "current-request"

    def model_request(request_id: str, generation: int, status: str) -> ModelRequest:
        running = status == "running"
        return ModelRequest(
            id=request_id,
            diagnostic_id=f"diagnostic-{request_id}",
            session_id=session_id,
            site_id="hh",
            stage="evaluation",
            role="job_summary",
            schema_ref="backend.intelligence.hirehi_category:JobSummary",
            status=status,
            generation=generation,
            attempt=1 if running else 0,
            max_attempts=4,
            model_id="provider/model",
            model_version="1",
            prompt_version="1",
            schema_version="1",
            parser_version="1",
            input_hash=("a" if generation == 1 else "c") * 64,
            cache_key=("b" if generation == 1 else "d") * 64,
            canonical_input=json.dumps({"job": {"title": request_id}}),
            created_at=now,
            available_at=now,
            started_at=now if running else None,
            heartbeat_at=now if running else None,
            deadline_at=now + timedelta(minutes=3),
            lease_owner="old-broker" if running else None,
        )

    with sessions() as db:
        session_id = add_session(db, "hh")
        execution = SessionExecution(session_id=session_id, generation=1)
        db.add(execution)
        db.add_all([
            model_request(old_running_id, 1, "running"),
            model_request(old_queued_id, 1, "queued"),
            model_request(current_id, 2, "queued"),
        ])
        db.add(PipelineModelOperation(
            session_id=session_id,
            site_id="hh",
            vacancy_key="vacancy-running",
            stage="evaluation",
            role="job_summary",
            input_hash="a" * 64,
            versions_hash="b" * 64,
            generation=1,
            request_id=old_running_id,
            status="running",
        ))
        execution.generation = 2
        assert PipelineStore.adopt_generation(db, session_id, "hh", 2)
        db.commit()

    provider_calls = []

    class Provider:
        async def generate(self, call):
            provider_calls.append(call.request_id)
            return JobSummary(summary="fresh generation")

        async def catalog_status(self):
            return {"connected": True}

    broker = ModelRequestBroker(sessions, provider=Provider())
    assert broker._complete(old_running_id, '{"summary":"stale"}') is False
    assert await broker.run_once() == 1
    await asyncio.gather(*tuple(broker._tasks.values()))

    with sessions() as db:
        old_running = db.get(ModelRequest, old_running_id)
        old_queued = db.get(ModelRequest, old_queued_id)
        current = db.get(ModelRequest, current_id)
        operation = db.scalar(select(PipelineModelOperation))
        assert (old_running.status, old_running.error_code) == ("cancelled", "cancelled")
        assert old_running.completed_at is not None
        assert old_running.lease_owner is None and old_running.heartbeat_at is None
        assert (old_queued.status, old_queued.error_code) == ("cancelled", "cancelled")
        assert old_queued.completed_at is not None
        assert current.status == "completed"
        assert (operation.generation, operation.status, operation.request_id) == (
            1, "cancelled", None
        )
    assert provider_calls == [current_id]


@pytest.mark.parametrize("terminal_status,cancel_requested", [("STOPPED", False), ("RUNNING", True)])
def test_generation_takeover_rejects_terminal_or_cancelled_execution(
    tmp_path, terminal_status, cancel_requested
):
    sessions = factory(tmp_path)
    with sessions() as db:
        session_id = add_session(db, "hh", status=terminal_status)
        execution = SessionExecution(
            session_id=session_id,
            generation=2,
            cancel_requested=cancel_requested,
        )
        db.add(execution)
        db.add(PipelineItem(
            session_id=session_id,
            site_id="hh",
            external_id="pending",
            stage="discovery",
            status="queued",
            generation=1,
        ))
        db.flush()
        assert PipelineStore.adopt_generation(db, session_id, "hh", 2) is False
        db.commit()
    with sessions() as db:
        item = db.scalar(select(PipelineItem))
        assert (item.generation, item.status) == (1, "queued")


@pytest.mark.parametrize("with_historical_vacancy", [False, True])
def test_completed_stage_is_terminal_idempotent_and_releases_backlog_capacity(
    tmp_path, with_historical_vacancy
):
    sessions = factory(tmp_path)
    pipeline = DurablePipelineCoordinator(sessions)
    active_refs = refs(10, "active")
    overflow = JobRef(external_id="backlog-next", url="https://example.test/backlog-next")
    with sessions() as db:
        session_id = add_session(db, "hh")
        historical_session_id = add_session(db, "hh", status="COMPLETED")
        db.add(SessionExecution(session_id=session_id, generation=2))
        target_vacancy = None
        if with_historical_vacancy:
            target_vacancy = Vacancy(
                session_id=historical_session_id,
                source="hh",
                external_id=active_refs[0].external_id,
                url=active_refs[0].url,
                title="Historical result",
                state="SUBMITTED",
                data={},
            )
            db.add(target_vacancy)
            db.flush()
        for index, ref in enumerate(active_refs):
            db.add(PipelineItem(
                session_id=session_id,
                site_id="hh",
                external_id=ref.external_id,
                vacancy_id=target_vacancy.id if index == 0 and target_vacancy else None,
                stage="evaluation",
                status="running",
                generation=2,
            ))
        db.add(PipelineCheckpoint(
            session_id=session_id,
            site_id="hh",
            name="discovery",
            generation=2,
            data={
                "backlog": [overflow.model_dump(mode="json")],
                "high_water": 10,
                "urls": {
                    **{ref.external_id: ref.url for ref in active_refs},
                    overflow.external_id: overflow.url,
                },
            },
        ))
        db.commit()

    assert pipeline.advance(
        session_id,
        "hh",
        active_refs[0].external_id,
        "completed",
        generation=2,
        vacancy_id=target_vacancy.id if target_vacancy else None,
    ) is True
    assert pipeline.advance(
        session_id,
        "hh",
        active_refs[0].external_id,
        "completed",
        generation=2,
    ) is False
    assert pipeline.advance(
        session_id,
        "hh",
        active_refs[0].external_id,
        "evaluation",
        generation=1,
    ) is False

    admitted = pipeline.enqueue(session_id, "hh", [], generation=2)
    assert len(admitted) == 10
    assert overflow.external_id in {item.external_id for item in admitted}
    with sessions() as db:
        completed = db.scalar(select(PipelineItem).where(
            PipelineItem.session_id == session_id,
            PipelineItem.external_id == active_refs[0].external_id,
        ))
        assert (completed.stage, completed.status) == ("completed", "completed")
        assert db.scalar(select(func.count(PipelineItem.id)).where(
            PipelineItem.session_id == session_id,
            PipelineItem.generation == 2,
            PipelineItem.status.in_(("queued", "running")),
            PipelineItem.stage.in_(("discovery", "extraction", "evaluation")),
        )) == 10
        checkpoint = db.scalar(select(PipelineCheckpoint).where(
            PipelineCheckpoint.session_id == session_id,
            PipelineCheckpoint.generation == 2,
        ))
        assert checkpoint.data["backlog"] == []
        if target_vacancy:
            historical = db.get(Vacancy, target_vacancy.id)
            assert (historical.session_id, historical.state) == (historical_session_id, "SUBMITTED")


@pytest.mark.asyncio
async def test_browser_steps_are_serial_per_site_while_model_steps_parallel(tmp_path):
    sessions = factory(tmp_path)
    pipeline = DurablePipelineCoordinator(sessions)
    active_browser = active_model = max_browser = max_model = 0

    async def browser():
        nonlocal active_browser, max_browser
        active_browser += 1
        max_browser = max(max_browser, active_browser)
        await asyncio.sleep(0.03)
        active_browser -= 1

    async def model():
        nonlocal active_model, max_model
        active_model += 1
        max_model = max(max_model, active_model)
        await asyncio.sleep(0.03)
        active_model -= 1

    await asyncio.gather(*[
        pipeline.browser_operation("hh", browser) for _ in range(3)
    ])
    await asyncio.gather(*[
        pipeline.model_operation(model) for _ in range(3)
    ])
    assert max_browser == 1
    assert max_model == 3


class RecordingProvider:
    def __init__(self, delay: float = 0.03) -> None:
        self.delay = delay
        self.calls: list[ProviderCall] = []
        self.active = Counter()
        self.max_global = 0
        self.max_session = Counter()

    async def generate(self, call: ProviderCall):
        self.calls.append(call)
        self.active[call.session_id] += 1
        self.max_global = max(self.max_global, sum(self.active.values()))
        self.max_session[call.session_id] = max(
            self.max_session[call.session_id], self.active[call.session_id]
        )
        await asyncio.sleep(self.delay)
        self.active[call.session_id] -= 1
        return JobSummary(summary=str(call.payload["job"]["description"]))

    async def catalog_status(self):
        return {"connected": True}


@pytest.mark.asyncio
async def test_brokered_workflow_calls_three_sessions_with_limits_full_payload_and_idempotency(tmp_path):
    sessions = factory(tmp_path, "brokered.db")
    with sessions() as db:
        ids = [add_session(db, site) for site in ("hh", "hirehi", "zarplata")]
    provider = RecordingProvider()
    broker = ModelRequestBroker(sessions, provider=provider)
    broker_task = asyncio.create_task(broker.run_forever(tick_seconds=0.005))
    gateways = [
        BrokeredModelGateway(session_id, site, 0, sessions, poll_interval=0.005)
        for session_id, site in zip(ids, ("hh", "hirehi", "zarplata"), strict=True)
    ]
    full_text = "resume-context:" + "x" * 20_000

    try:
        results = await asyncio.gather(*[
            gateway.structured(
                "job_summary",
                {
                    "job": {"description": f"{full_text}:{index}"},
                    "resume": {"schema_version": 2, "extractor_version": "parser-17"},
                },
                JobSummary,
            )
            for index, gateway in enumerate(gateways)
        ])
        # A recovered worker reuses the same durable operation/request rather
        # than making a second provider call.
        repeated = await BrokeredModelGateway(
            ids[0], "hh", 0, sessions, poll_interval=0.005
        ).structured(
            "job_summary",
            {
                "job": {"description": f"{full_text}:0"},
                "resume": {"schema_version": 2, "extractor_version": "parser-17"},
            },
            JobSummary,
        )
    finally:
        await broker.stop(drain=True)
        await broker_task

    assert all(result.summary.startswith("resume-context:") for result in results)
    assert repeated.summary == results[0].summary
    assert len(provider.calls) == 3
    assert provider.max_global == 3
    assert all(value <= 2 for value in provider.max_session.values())
    assert {call.site_id for call in provider.calls} == {"hh", "hirehi", "zarplata"}
    assert all(len(call.payload["job"]["description"]) > 20_000 for call in provider.calls)
    with sessions() as db:
        requests = list(db.scalars(select(ModelRequest)))
        assert len(requests) == 3
        assert all(row.diagnostic_id and row.parser_version for row in requests)
        assert all("resume-context:" in row.canonical_input for row in requests)
        assert db.scalar(select(func.count(PipelineModelOperation.id))) == 3


@pytest.mark.asyncio
async def test_cancellation_fences_running_model_and_late_result(tmp_path):
    sessions = factory(tmp_path, "cancel.db")
    with sessions() as db:
        session_id = add_session(db, "hh")
    provider = RecordingProvider(delay=0.15)
    broker = ModelRequestBroker(sessions, provider=provider)
    task = asyncio.create_task(broker.run_forever(tick_seconds=0.005))
    gateway = BrokeredModelGateway(session_id, "hh", 0, sessions, poll_interval=0.005)
    call = asyncio.create_task(gateway.structured(
        "job_summary", {"job": {"description": "cancel me"}}, JobSummary
    ))
    try:
        for _ in range(100):
            with sessions() as db:
                row = db.scalar(select(ModelRequest))
                if row is not None and row.status == "running":
                    db.get(JobSession, session_id).status = "STOPPING"
                    db.commit()
                    break
            await asyncio.sleep(0.005)
        with pytest.raises(asyncio.CancelledError):
            await call
        await asyncio.sleep(0.2)
    finally:
        await broker.stop(drain=True)
        await task
    with sessions() as db:
        assert db.scalar(select(ModelRequest.status)) == "cancelled"
        assert db.scalar(select(PipelineModelOperation.status)) == "cancelled"
