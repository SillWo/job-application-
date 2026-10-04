from __future__ import annotations

from queue import Queue

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from backend.adapters.base.protocol import JobRef
from backend.orchestrator.pipeline import DurablePipelineCoordinator, PipelineStore
from backend.persistence.database import Base
from backend.persistence.execution_models import SessionExecution
from backend.persistence.models import JobSession, Vacancy
from backend.persistence.pipeline_models import PipelineItem  # noqa: F401
from backend.runtime.ipc import WorkerCommand
from backend.runtime.supervisor import RuntimeSupervisor
from backend.schemas.domain import SessionStatus


class _StartedProcess:
    pid = 4242

    def __init__(self, *, target, args, daemon):
        self.target = target
        self.args = args
        self.daemon = daemon
        self.started = False

    def start(self):
        self.started = True

    def is_alive(self):
        return self.started


def _runtime_db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'runtime-feedback.db'}")
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine, expire_on_commit=False)


def _fake_context(supervisor, process_calls):
    class Context:
        @staticmethod
        def Queue(maxsize):
            return Queue(maxsize=maxsize)

        @staticmethod
        def Process(*, target, args, daemon):
            process = _StartedProcess(target=target, args=args, daemon=daemon)
            process_calls.append(process)
            return process

    supervisor.ctx = Context()


def test_takeover_adopts_generation_and_queued_work_before_spawning(tmp_path):
    from backend.adapters.base.protocol import JobRef

    engine, sessions = _runtime_db(tmp_path)
    with sessions() as db:
        item = JobSession(adapter_id="hh", status=SessionStatus.RUNNING)
        db.add(item)
        db.flush()
        db.add(SessionExecution(session_id=item.id, generation=1, stage="RUNNING"))
        db.commit()
        session_id = item.id
    pipeline = DurablePipelineCoordinator(sessions)
    refs = [JobRef(external_id=f"job-{index}", url=f"https://hh.ru/vacancy/{index}") for index in range(15)]
    assert len(pipeline.enqueue(session_id, "hh", refs, generation=1)) == 10

    supervisor = RuntimeSupervisor(session_factory=sessions)
    process_calls = []
    _fake_context(supervisor, process_calls)
    handle = supervisor.start(site_id="hh", session_id=session_id)

    assert handle is not None and handle.generation == 2
    assert len(process_calls) == 1 and process_calls[0].started
    command = handle.commands.receive()
    assert isinstance(command, WorkerCommand) and command.command == "START"
    assert command.generation == 2
    with sessions() as db:
        execution = db.scalar(select(SessionExecution).where(SessionExecution.session_id == session_id))
        active = list(db.scalars(select(PipelineItem).where(
            PipelineItem.session_id == session_id,
            PipelineItem.generation == 2,
            PipelineItem.status.in_(("queued", "running")),
        )))
        assert execution.generation == 2
        assert len(active) == 10
        assert len({row.external_id for row in active}) == 10
    engine.dispose()


def test_failed_pipeline_adoption_rolls_back_generation_and_never_spawns(tmp_path, monkeypatch):
    engine, sessions = _runtime_db(tmp_path)
    with sessions() as db:
        item = JobSession(adapter_id="hirehi", status=SessionStatus.RUNNING)
        db.add(item)
        db.flush()
        db.add(SessionExecution(session_id=item.id, generation=1, stage="RUNNING"))
        db.commit()
        session_id = item.id

    def fail_adoption(*_args, **_kwargs):
        raise RuntimeError("pipeline adoption failed")

    monkeypatch.setattr(PipelineStore, "adopt_generation", fail_adoption)
    supervisor = RuntimeSupervisor(session_factory=sessions)
    process_calls = []
    _fake_context(supervisor, process_calls)
    with pytest.raises(RuntimeError, match="pipeline adoption failed"):
        supervisor.start(site_id="hirehi", session_id=session_id)

    assert process_calls == []
    assert supervisor.generations == {}
    with sessions() as db:
        assert db.scalar(select(SessionExecution).where(SessionExecution.session_id == session_id)).generation == 1
    engine.dispose()


def test_database_failure_does_not_fall_back_to_an_unfenced_generation(tmp_path):
    class FailingFactory:
        def __call__(self):
            return self

        def __enter__(self):
            raise RuntimeError("db unavailable")

        def __exit__(self, *_args):
            return False

    supervisor = RuntimeSupervisor(session_factory=FailingFactory())
    process_calls = []
    _fake_context(supervisor, process_calls)

    with pytest.raises(RuntimeError, match="db unavailable"):
        supervisor.start(site_id="hh", session_id=901)

    assert process_calls == []
    assert supervisor.generations == {}


def test_historical_and_current_session_duplicates_release_pipeline_capacity(tmp_path, monkeypatch):
    from backend.orchestrator import workflow

    engine, sessions = _runtime_db(tmp_path)
    with sessions() as db:
        historical = JobSession(adapter_id="hh", status=SessionStatus.COMPLETED, counters={})
        current = JobSession(
            adapter_id="hh", status=SessionStatus.RUNNING, counters={
                "viewed": 0, "filtered": 0, "matched": 0, "submitted": 0,
                "reported": 0, "already_applied": 0, "errors": 0,
            }, recovery={},
        )
        db.add_all([historical, current])
        db.flush()
        db.add(SessionExecution(session_id=current.id, generation=1, stage="RUNNING"))
        old_vacancies = [
            Vacancy(
                session_id=historical.id,
                source="hh",
                external_id=f"old-{index}",
                url=f"https://hh.ru/vacancy/old-{index}",
                title=f"Old role {index}",
                state="REPORTED",
                data={"keep": index},
            )
            for index in range(10)
        ]
        db.add_all(old_vacancies)
        db.commit()
        session_id = current.id
        old_ids = [vacancy.id for vacancy in old_vacancies]

    monkeypatch.setattr(workflow, "SessionLocal", sessions)
    manager = workflow.WorkflowManager(generation=1)
    initial_refs = [
        JobRef(external_id=f"old-{index}", url=f"https://hh.ru/vacancy/old-{index}")
        for index in range(10)
    ] + [
        JobRef(external_id=f"new-{index}", url=f"https://hh.ru/vacancy/new-{index}")
        for index in range(5)
    ]
    assert len(manager._save_refs(session_id, initial_refs)) == 10

    for external_id, vacancy_id in zip(
        (f"old-{index}" for index in range(10)), old_ids, strict=True
    ):
        assert manager._complete_duplicate_pipeline_item(
            session_id, "hh", external_id, vacancy_id
        )
    promoted = manager._save_refs(session_id, [], adapter=None)
    assert [ref.external_id for ref in promoted] == [f"new-{i}" for i in range(5)]

    with sessions() as db:
        assert [db.get(Vacancy, vacancy_id).data for vacancy_id in old_ids] == [
            {"keep": index} for index in range(10)
        ]
        assert db.get(JobSession, session_id).counters["submitted"] == 0
        db.add(Vacancy(
            session_id=session_id,
            source="hh",
            external_id="repeat-current",
            url="https://hh.ru/vacancy/repeat-current",
            title="Already handled this session",
            state="REPORTED",
            data={"keep": True},
        ))
        db.commit()
    pipeline = DurablePipelineCoordinator(sessions)
    repeated = JobRef(external_id="repeat-current", url="https://hh.ru/vacancy/repeat-current")
    assert len(pipeline.enqueue(session_id, "hh", [repeated], generation=1)) == 6
    with sessions() as db:
        repeated_vacancy = db.scalar(select(Vacancy).where(
            Vacancy.session_id == session_id,
            Vacancy.external_id == "repeat-current",
        ))
    assert manager._complete_duplicate_pipeline_item(
        session_id, "hh", repeated.external_id, repeated_vacancy.id
    )
    remaining = manager._save_refs(session_id, [], adapter=None)
    assert "repeat-current" not in {ref.external_id for ref in remaining}
    with sessions() as db:
        repeated_item = db.scalar(select(PipelineItem).where(
            PipelineItem.session_id == session_id,
            PipelineItem.external_id == "repeat-current",
            PipelineItem.generation == 1,
        ))
        assert repeated_item.status == "completed"
        assert repeated_item.vacancy_id == repeated_vacancy.id
    engine.dispose()

