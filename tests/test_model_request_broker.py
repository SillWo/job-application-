from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from backend.intelligence.gateway import ModelGateway, ModelPermanentError
from backend.intelligence.hirehi_category import JobSummary
from backend.intelligence.model_broker import (
    ModelRequestBroker,
    ModelRequestClient,
    ModelRequestFailed,
    ModelVersions,
    SubmitRequest,
)
from backend.persistence import execution_models, model_request_models, models  # noqa: F401
from backend.persistence.database import Base
from backend.persistence.model_request_models import ModelRequest, ModelResponseCache
from backend.persistence.models import JobSession


@pytest.fixture
def broker_db(tmp_path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'broker.db').as_posix()}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as db:
        db.add_all(
            [
                JobSession(id=1, adapter_id="hh", status="RUNNING", counters={}),
                JobSession(id=2, adapter_id="hirehi", status="RUNNING", counters={}),
                JobSession(id=3, adapter_id="zarplata", status="RUNNING", counters={}),
            ]
        )
        db.commit()
    yield factory
    engine.dispose()


VERSIONS = ModelVersions(
    model_id="provider/model",
    model_version="2026-09",
    prompt_version="prompt-7",
    schema_version="schema-3",
    parser_version="parser-11",
)


def request(session_id: int, site_id: str, marker: str, *, versions=VERSIONS) -> SubmitRequest:
    return SubmitRequest(
        session_id=session_id,
        site_id=site_id,
        vacancy_id=f"vacancy-{marker}",
        stage="evaluation",
        role="job_summary",
        payload={"job": {"title": marker}},
        schema=JobSummary,
        versions=versions,
    )


class BlockingProvider:
    def __init__(self, expected: int) -> None:
        self.expected = expected
        self.release = asyncio.Event()
        self.entered = asyncio.Event()
        self.seen = []
        self.active = 0
        self.max_active = 0
        self.per_session = {}
        self.max_per_session = {}

    async def generate(self, call):
        self.seen.append(call)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.per_session[call.session_id] = self.per_session.get(call.session_id, 0) + 1
        self.max_per_session[call.session_id] = max(
            self.max_per_session.get(call.session_id, 0), self.per_session[call.session_id]
        )
        if len(self.seen) >= self.expected:
            self.entered.set()
        try:
            await self.release.wait()
            return {"summary": call.payload["job"]["title"]}
        finally:
            self.active -= 1
            self.per_session[call.session_id] -= 1

    async def catalog_status(self):
        return {"connected": True, "model_available": False, "source": "catalog"}


async def finish_active(broker: ModelRequestBroker) -> None:
    tasks = tuple(broker._tasks.values())
    if tasks:
        await asyncio.gather(*tasks)
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_scheduler_is_global_three_fair_by_site_and_fifo(broker_db):
    provider = BlockingProvider(expected=3)
    broker = ModelRequestBroker(broker_db, provider=provider)
    receipts = [
        broker.submit(request(1, "hh", "hh-1")),
        broker.submit(request(1, "hh", "hh-2")),
        broker.submit(request(1, "hh", "hh-3")),
        broker.submit(request(2, "hirehi", "hirehi-1")),
        broker.submit(request(3, "zarplata", "zarplata-1")),
    ]

    assert await broker.run_once() == 3
    await asyncio.wait_for(provider.entered.wait(), timeout=1)
    assert provider.max_active == 3
    assert {item.site_id for item in provider.seen} == {"hh", "hirehi", "zarplata"}, [
        item.site_id for item in provider.seen
    ]
    assert provider.seen[0].request_id == receipts[0].request_id
    provider.release.set()
    await finish_active(broker)

    await broker.drain()
    hh_calls = [item for item in provider.seen if item.site_id == "hh"]
    assert [item.request_id for item in hh_calls] == [item.request_id for item in receipts[:3]]
    assert all(broker.poll(item.request_id).status == "completed" for item in receipts)


@pytest.mark.asyncio
async def test_scheduler_never_runs_more_than_two_for_one_session(broker_db):
    provider = BlockingProvider(expected=2)
    broker = ModelRequestBroker(broker_db, provider=provider)
    receipts = [
        broker.submit(request(1, "hh", "one")),
        broker.submit(request(1, "hirehi", "two")),
        broker.submit(request(1, "zarplata", "three")),
    ]

    assert await broker.run_once() == 2
    await asyncio.wait_for(provider.entered.wait(), timeout=1)
    assert provider.max_per_session[1] == 2
    assert broker.poll(receipts[2].request_id).status == "queued"
    provider.release.set()
    await finish_active(broker)
    await broker.drain()
    assert broker.poll(receipts[2].request_id).status == "completed"


@pytest.mark.asyncio
async def test_capacity_blocked_site_head_does_not_starve_other_session(broker_db):
    provider = BlockingProvider(expected=1)
    broker = ModelRequestBroker(broker_db, provider=provider)
    blockers = [
        broker.submit(request(1, "hirehi", "blocker-1")),
        broker.submit(request(1, "zarplata", "blocker-2")),
    ]
    blocked_head = broker.submit(request(1, "hh", "blocked-head"))
    runnable = broker.submit(request(2, "hh", "runnable"))
    with broker_db() as db:
        for receipt in blockers:
            row = db.get(ModelRequest, receipt.request_id)
            row.status = "running"
            row.attempt = 1
            row.started_at = datetime.now(timezone.utc)
            row.heartbeat_at = datetime.now(timezone.utc)
            row.lease_owner = "other-central-broker"
        db.commit()

    assert await broker.run_once() == 1
    await asyncio.wait_for(provider.entered.wait(), timeout=1)
    assert provider.seen[0].request_id == runnable.request_id
    assert broker.poll(blocked_head.request_id).status == "queued"
    provider.release.set()
    await finish_active(broker)


@pytest.mark.asyncio
async def test_cache_is_session_local_complete_and_versioned(broker_db):
    class Provider:
        def __init__(self):
            self.calls = 0

        async def generate(self, call):
            self.calls += 1
            return {"summary": f"generated-{self.calls}"}

        async def catalog_status(self):
            return {"model_available": True}

    provider = Provider()
    broker = ModelRequestBroker(broker_db, provider=provider)
    long_resume = "FULL-RESUME-" + ("опыт " * 4_000)
    base = replace(request(1, "hh", "cache"), payload={"resume": long_resume})
    first = broker.submit(base)
    await broker.drain()
    assert provider.calls == 1

    cached = broker.submit(base)
    assert cached.cache_hit is True
    assert broker.poll(cached.request_id).result == {"summary": "generated-1"}
    different_role = broker.submit(replace(base, role="different_model_operation"))
    assert different_role.cache_hit is False
    other_session = broker.submit(replace(base, session_id=2, site_id="hirehi"))
    assert other_session.cache_hit is False

    for field in (
        "model_id",
        "model_version",
        "prompt_version",
        "schema_version",
        "parser_version",
    ):
        changed_versions = replace(VERSIONS, **{field: getattr(VERSIONS, field) + "-new"})
        receipt = broker.submit(replace(base, versions=changed_versions))
        assert receipt.cache_hit is False, field

    with broker_db() as db:
        row = db.get(ModelRequest, first.request_id)
        assert long_resume in row.canonical_input
        assert len(row.canonical_input) > len(long_resume)
        cache_rows = list(db.scalars(select(ModelResponseCache)))
        assert [(item.session_id, item.source_request_id) for item in cache_rows] == [
            (1, first.request_id)
        ]


def test_canonical_storage_redacts_credentials_without_truncating_resume(broker_db):
    client = ModelRequestClient(broker_db)
    payload = {
        "resume": "complete resume " * 2_000,
        "api_key": "sk-this-must-never-be-stored-123456",
        "nested": {
            "authorization": "Bearer very-secret-token-123456",
            "clientSecret": "ordinary-value-without-a-provider-prefix",
            "token": "another-plain-credential",
            "note": "proxy_password=credential-inside-free-text",
        },
    }
    receipt = client.submit(replace(request(1, "hh", "secret"), payload=payload))
    with broker_db() as db:
        stored = db.get(ModelRequest, receipt.request_id).canonical_input
    assert "sk-this-must-never-be-stored" not in stored
    assert "very-secret-token" not in stored
    assert "ordinary-value-without-a-provider-prefix" not in stored
    assert "another-plain-credential" not in stored
    assert "credential-inside-free-text" not in stored
    assert stored.count("complete resume") == 2_000


def test_request_payload_must_be_json_object(broker_db):
    client = ModelRequestClient(broker_db)
    with pytest.raises(ValueError, match="JSON object"):
        client.submit(replace(request(1, "hh", "bad-root"), payload=["not", "an", "object"]))


class MutableClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 9, 23, tzinfo=timezone.utc)

    def __call__(self):
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += timedelta(seconds=seconds)


@pytest.mark.asyncio
async def test_retry_uses_bounded_backoff_and_same_logical_deadline(broker_db):
    class Flaky:
        def __init__(self):
            self.calls = []

        async def generate(self, call):
            self.calls.append(call)
            if len(self.calls) == 1:
                raise ConnectionError("provider response body must not be persisted")
            return {"summary": "ok"}

        async def catalog_status(self):
            return {}

    clock = MutableClock()
    provider = Flaky()
    broker = ModelRequestBroker(broker_db, provider=provider, clock=clock)
    receipt = broker.submit(request(1, "hh", "retry"))
    original_deadline = receipt.deadline_at

    await broker.run_once()
    await finish_active(broker)
    state = broker.poll(receipt.request_id)
    assert state.status == "retry" and state.attempt == 1
    with broker_db() as db:
        row = db.get(ModelRequest, receipt.request_id)
        assert row.available_at == clock.value.replace(tzinfo=None) + timedelta(seconds=5)
        assert "provider response body" not in (row.error_code or "")

    clock.advance(5)
    await broker.run_once()
    await finish_active(broker)
    state = broker.poll(receipt.request_id)
    assert state.status == "completed" and state.attempt == 2
    assert state.deadline_at == original_deadline
    assert provider.calls[1].remaining_seconds == pytest.approx(175)


@pytest.mark.asyncio
async def test_deadline_is_terminal_and_generation_health_is_not_catalog_status(broker_db):
    class Slow:
        async def generate(self, call):
            await asyncio.sleep(0.6)
            return {"summary": "late"}

        async def catalog_status(self):
            return {"connected": True, "model_available": False, "source": "catalog"}

    broker = ModelRequestBroker(
        broker_db,
        provider=Slow(),
        logical_deadline_seconds=0.5,
    )
    receipt = broker.submit(request(1, "hh", "deadline"))
    await broker.run_once()
    await finish_active(broker)
    state = broker.poll(receipt.request_id)
    assert state.status == "failed"
    assert state.error_code == "model_timeout"
    assert await broker.catalog_availability() == {
        "connected": True,
        "model_available": False,
        "source": "catalog",
    }
    health = broker.generation_health()
    assert health["healthy"] is False
    assert health["failure_count"] == 1


@pytest.mark.asyncio
async def test_permanent_provider_validation_error_is_terminal(broker_db):
    class InvalidProvider:
        def __init__(self):
            self.calls = 0

        async def generate(self, call):
            self.calls += 1
            raise ModelPermanentError("invalid schema output")

        async def catalog_status(self):
            return {}

    provider = InvalidProvider()
    broker = ModelRequestBroker(broker_db, provider=provider)
    receipt = broker.submit(request(1, "hh", "permanent"))

    await broker.run_once()
    await finish_active(broker)
    state = broker.poll(receipt.request_id)

    assert state.status == "failed"
    assert state.error_code == "permanent_model_error"
    assert state.attempt == provider.calls == 1


def test_recovery_is_idempotent_and_never_replays_completed(broker_db):
    clock = MutableClock()

    class Provider:
        async def generate(self, call):  # pragma: no cover - recovery does not invoke providers
            raise AssertionError

        async def catalog_status(self):
            return {}

    broker = ModelRequestBroker(broker_db, provider=Provider(), clock=clock)
    stale = broker.submit(request(1, "hh", "stale"))
    completed = broker.submit(request(2, "hirehi", "done"))
    with broker_db() as db:
        stale_row = db.get(ModelRequest, stale.request_id)
        stale_row.status = "running"
        stale_row.attempt = 1
        stale_row.started_at = clock.value - timedelta(minutes=2)
        stale_row.heartbeat_at = clock.value - timedelta(minutes=2)
        stale_row.lease_owner = "dead-process"
        done_row = db.get(ModelRequest, completed.request_id)
        done_row.status = "completed"
        done_row.canonical_output = '{"summary":"already done"}'
        done_row.completed_at = clock.value - timedelta(minutes=1)
        db.commit()

    assert broker.recover_stale(stale_after_seconds=30) == 1
    assert broker.recover_stale(stale_after_seconds=30) == 0
    assert broker.poll(stale.request_id).status == "retry"
    with broker_db() as db:
        assert db.get(ModelRequest, stale.request_id).available_at == (
            clock.value + timedelta(seconds=5)
        ).replace(tzinfo=None)
    done = broker.poll(completed.request_id)
    assert done.status == "completed"
    assert done.result == {"summary": "already done"}


@pytest.mark.asyncio
async def test_periodic_recovery_waits_for_live_heartbeat_then_recovers(broker_db):
    clock = MutableClock()

    class Provider:
        async def generate(self, call):  # pragma: no cover - request remains in backoff
            raise AssertionError

        async def catalog_status(self):
            return {}

    broker = ModelRequestBroker(broker_db, provider=Provider(), clock=clock)
    receipt = broker.submit(request(1, "hh", "fresh-lease"))
    with broker_db() as db:
        row = db.get(ModelRequest, receipt.request_id)
        row.status = "running"
        row.attempt = 1
        row.started_at = clock.value
        row.heartbeat_at = clock.value
        row.lease_owner = "crashed-owner"
        db.commit()

    service = asyncio.create_task(
        broker.run_forever(tick_seconds=0.001, stale_after_seconds=30)
    )
    await asyncio.sleep(0.01)
    assert broker.poll(receipt.request_id).status == "running"
    clock.advance(31)
    for _ in range(100):
        if broker.poll(receipt.request_id).status == "retry":
            break
        await asyncio.sleep(0.001)
    assert broker.poll(receipt.request_id).status == "retry"
    await broker.stop()
    await service


@pytest.mark.asyncio
async def test_worker_wait_persists_terminal_deadline_without_broker_tick(broker_db):
    clock = MutableClock()
    client = ModelRequestClient(broker_db, clock=clock)
    receipt = client.submit(request(1, "hh", "worker-timeout"))
    clock.advance(181)
    with pytest.raises(ModelRequestFailed) as raised:
        await client.wait(receipt.request_id, poll_interval=0)
    assert raised.value.state.status == "failed"
    state = client.poll(receipt.request_id)
    assert state.status == "failed"
    assert state.error_code == "deadline_exceeded"


@pytest.mark.asyncio
async def test_worker_wait_returns_completion_won_at_deadline_race(broker_db):
    base_clock = MutableClock()
    client = ModelRequestClient(broker_db, clock=base_clock)
    receipt = client.submit(request(1, "hh", "deadline-race"))
    completed = False

    def completing_clock():
        nonlocal completed
        if not completed:
            completed = True
            with broker_db() as db:
                row = db.get(ModelRequest, receipt.request_id)
                row.status = "completed"
                row.canonical_output = '{"summary":"won race"}'
                row.completed_at = base_clock.value + timedelta(seconds=181)
                db.commit()
        return base_clock.value + timedelta(seconds=181)

    client._clock = completing_clock
    state = await client.wait(receipt.request_id, poll_interval=0)
    assert state.status == "completed"
    assert state.result == {"summary": "won race"}


def test_diagnostic_id_and_request_context_are_durable(broker_db):
    client = ModelRequestClient(broker_db)
    receipt = client.submit(
        replace(request(1, "hh", "context"), generation=7, stage="cover_letter")
    )
    state = client.poll(receipt.request_id)
    assert state.diagnostic_id == receipt.diagnostic_id
    assert state.session_id == 1
    assert state.site_id == "hh"
    assert state.vacancy_id == "vacancy-context"
    assert state.stage == "cover_letter"
    assert state.generation == 7


@pytest.mark.asyncio
async def test_gateway_schema_error_uses_bounded_format_repair(monkeypatch):
    calls = 0
    request_headers = []

    async def delayed_completion(*args, **kwargs):
        nonlocal calls
        calls += 1
        request_headers.append(kwargs.get("extra_headers"))
        # Leave enough room for the first response to be decoded, then make
        await asyncio.sleep(0.02)
        content = "{}"
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
            usage=None,
        )

    saved = SimpleNamespace(
        base_url="http://127.0.0.1:8045/v1",
        model="local-test-model",
        encrypted_api_key="",
    )

    class FakeOpenAI:
        def __init__(self, **kwargs):
            pass

    class FakeTransport:
        async def aclose(self):
            pass

    monkeypatch.setattr(ModelGateway, "_saved_config", staticmethod(lambda: saved))
    monkeypatch.setattr("backend.intelligence.gateway.AsyncOpenAI", FakeOpenAI)
    monkeypatch.setattr(
        "backend.intelligence.gateway.model_http_client", lambda *args: FakeTransport()
    )
    monkeypatch.setattr(
        "backend.intelligence.gateway.create_completion", delayed_completion
    )
    started = time.monotonic()
    with pytest.raises(ModelPermanentError):
        await ModelGateway("openai_compat").direct_structured(
            "job_summary",
            {},
            JobSummary,
            logical_timeout=0.5,
            diagnostic_id="f05e8a57-5354-4d0d-874d-b8262e638594",
        )
    elapsed = time.monotonic() - started
    assert calls == 2
    assert request_headers == [
        {"X-Client-Request-Id": "f05e8a57-5354-4d0d-874d-b8262e638594"},
        {"X-Client-Request-Id": "f05e8a57-5354-4d0d-874d-b8262e638594"},
    ]
    assert elapsed < 0.5
