"""Exercise outages and crashes through durable workflow state, without live submissions."""
import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from test_workflow_non_captcha_continuation import FakeAdapter, evaluation
from test_workflow_non_captcha_continuation import runtime as recovery_runtime

from backend.adapters.base.protocol import ApplicationForm, Blocker, JobRef, SubmissionResult
from backend.orchestrator import workflow
from backend.persistence.models import Application, BrowserEvent, JobSession, Vacancy
from backend.schemas.domain import SessionStatus

runtime = recovery_runtime


def refs(*ids):
    return [JobRef(external_id=value, url=f"https://fake/{value}") for value in ids]


def configure(monkeypatch, adapter):
    async def apply(*args, **kwargs):
        return evaluation("apply")

    async def letter(*args, **kwargs):
        return "Fixture cover letter"

    monkeypatch.setattr(workflow.adapter_registry, "get", lambda _: adapter)
    monkeypatch.setattr(workflow, "evaluate", apply)
    monkeypatch.setattr(workflow, "write_cover_letter", letter)


@pytest.mark.asyncio
async def test_failed_extraction_is_retried_even_if_listing_loses_the_ref(runtime, monkeypatch):
    class Adapter(FakeAdapter):
        attempts = 0
        searches = 0
        submitted = []

        async def collect_job_refs(self, page):
            self.searches += 1
            return self.refs if self.searches == 1 else []

        async def extract_job(self, page):
            if self.current_ref.external_id == "slow":
                self.attempts += 1
                if self.attempts == 1:
                    raise TimeoutError("slow page")
            return await super().extract_job(page)

        async def submit_application(self, page):
            self.submitted.append(self.current_ref.external_id)
            return await super().submit_application(page)

    adapter = Adapter(refs("slow", "healthy"))
    configure(monkeypatch, adapter)
    await asyncio.wait_for(workflow.WorkflowManager().run(runtime[1]), timeout=5)
    assert adapter.submitted == ["healthy", "slow"]
    with runtime[0]() as db:
        item = db.get(JobSession, runtime[1])
        assert item.status == SessionStatus.COMPLETED
        assert item.counters["submitted"] == item.counters["viewed"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["open", "submit"])
async def test_process_crash_after_send_is_reconciled_without_second_click(runtime, monkeypatch, phase):
    server = {"sent": False, "clicks": 0}

    class Adapter(FakeAdapter):
        async def can_retry_application(self, page):
            return not server["sent"]

        async def verify_submission(self, page):
            return SubmissionResult(status="already_applied" if server["sent"] else "unknown", message="server state")

        async def open_application(self, page):
            if phase == "open":
                server.update(sent=True, clicks=server["clicks"] + 1)
                raise asyncio.CancelledError()
            return ApplicationForm()

        async def submit_application(self, page):
            server.update(sent=True, clicks=server["clicks"] + 1)
            raise asyncio.CancelledError()

    configure(monkeypatch, Adapter(refs("one")))
    with pytest.raises(asyncio.CancelledError):
        await workflow.WorkflowManager().run(runtime[1])
    with runtime[0]() as db:
        assert db.scalar(select(Vacancy)).state == "SUBMITTING"
        assert db.get(JobSession, runtime[1]).counters["submitted"] == 0

    # New process/adapter: only DB state and site-visible confirmation survive.
    configure(monkeypatch, Adapter(refs("one")))
    await asyncio.wait_for(workflow.WorkflowManager().run(runtime[1]), timeout=5)
    with runtime[0]() as db:
        assert db.get(JobSession, runtime[1]).status == SessionStatus.COMPLETED
        assert db.get(JobSession, runtime[1]).counters["submitted"] == 1
        assert len(list(db.scalars(select(Application)))) == 1
    assert server["clicks"] == 1


@pytest.mark.asyncio
async def test_form_timeout_before_send_retries_when_site_confirms_no_application(runtime, monkeypatch):
    class Adapter(FakeAdapter):
        attempts = 0

        async def can_retry_application(self, page):
            return True

        async def open_application(self, page):
            self.attempts += 1
            if self.attempts == 1:
                raise TimeoutError("form unavailable")
            return ApplicationForm()

    adapter = Adapter(refs("one"))
    configure(monkeypatch, adapter)
    await asyncio.wait_for(workflow.WorkflowManager().run(runtime[1]), timeout=5)
    assert adapter.attempts == 2
    with runtime[0]() as db:
        assert db.get(JobSession, runtime[1]).counters["submitted"] == 1


@pytest.mark.asyncio
async def test_captcha_inside_form_pauses_without_sending(runtime, monkeypatch):
    class Adapter(FakeAdapter):
        captcha = False
        submitted = False

        async def open_application(self, page):
            self.captcha = True
            return ApplicationForm()

        async def detect_blockers(self, page):
            return [Blocker(kind="captcha", message="captcha")] if self.captcha else []

        async def submit_application(self, page):
            self.submitted = True
            return await super().submit_application(page)

    adapter = Adapter(refs("one"))
    configure(monkeypatch, adapter)
    await workflow.WorkflowManager().run(runtime[1])
    assert not adapter.submitted
    with runtime[0]() as db:
        assert db.get(JobSession, runtime[1]).status == SessionStatus.PAUSED
        assert db.get(JobSession, runtime[1]).counters["submitted"] == 0


@pytest.mark.asyncio
async def test_stop_interrupts_backoff_and_preserves_user_stop(runtime, monkeypatch):
    manager = workflow.WorkflowManager()
    manager.retry_base_seconds = 60
    calls = 0

    async def fail(session_id):
        nonlocal calls
        calls += 1
        raise workflow.ModelUnavailable("offline")

    monkeypatch.setattr(manager, "_run", fail)
    task = asyncio.create_task(manager.run(runtime[1]))
    for _ in range(100):
        with runtime[0]() as db:
            item = db.get(JobSession, runtime[1])
            if (item.recovery or {}).get("retry_at"):
                assert item.status == SessionStatus.RUNNING
                item.status = SessionStatus.STOPPED
                item.stop_reason = "user stop"
                db.commit()
                break
        await asyncio.sleep(0.01)
    await asyncio.wait_for(task, timeout=1)
    assert calls == 1
    with runtime[0]() as db:
        assert db.get(JobSession, runtime[1]).stop_reason == "user stop"


def test_startup_recovers_only_accepted_active_work(runtime, monkeypatch):
    sessions, session_id = runtime
    with sessions() as db:
        item = db.get(JobSession, session_id)
        item.status = SessionStatus.RUNNING
        for status in (SessionStatus.CREATED, SessionStatus.PAUSED, SessionStatus.STOPPED, SessionStatus.COMPLETED):
            db.add(JobSession(profile_id=item.profile_id, adapter_id="fake", status=status))
        db.commit()
    launched = []
    monkeypatch.setattr(workflow.workflow_manager, "launch", lambda ident: launched.append(ident))
    assert workflow.recover_orphaned_sessions() == [session_id]
    assert launched == [session_id]


@pytest.mark.asyncio
async def test_browser_is_automatically_restored(runtime, monkeypatch):
    adapter = FakeAdapter(refs("one"))
    configure(monkeypatch, adapter)
    restored = []

    async def restore(session_id, adapter):
        restored.append(session_id)
        return SimpleNamespace(page=object())

    monkeypatch.setattr(workflow, "get_browser", lambda _: None)
    monkeypatch.setattr(workflow, "restore_browser", restore)
    await workflow.WorkflowManager().run(runtime[1])
    assert restored == [runtime[1]]


@pytest.mark.asyncio
async def test_target_reached_does_not_depend_on_model_or_browser(runtime, monkeypatch):
    with runtime[0]() as db:
        item = db.get(JobSession, runtime[1])
        item.started_at = workflow.datetime.now(workflow.timezone.utc)
        item.application_limit = 100
        item.counters = {"submitted": 100}
        db.commit()
    monkeypatch.setattr(workflow, "get_browser", lambda _: pytest.fail("already at goal"))
    await workflow.WorkflowManager().run(runtime[1])
    with runtime[0]() as db:
        assert db.get(JobSession, runtime[1]).status == SessionStatus.COMPLETED


@pytest.mark.asyncio
async def test_preference_compilation_outage_is_retried_inside_workflow(runtime, monkeypatch):
    from backend.schemas.domain import DesiredJobPolicy

    configure(monkeypatch, FakeAdapter(refs("one")))
    with runtime[0]() as db:
        db.get(JobSession, runtime[1]).desired_job_description = "GameDev"
        db.commit()
    calls = 0

    async def compile_policy(*args):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise workflow.ModelUnavailable("offline")
        return DesiredJobPolicy()

    monkeypatch.setattr(workflow, "compile_preference_policy", compile_policy)
    await asyncio.wait_for(workflow.WorkflowManager().run(runtime[1]), timeout=5)
    with runtime[0]() as db:
        assert db.get(JobSession, runtime[1]).preference_policy is not None
        assert db.get(JobSession, runtime[1]).counters["submitted"] == 1
        assert len(list(db.scalars(select(BrowserEvent).where(BrowserEvent.event_type == "recovery_retry")))) == 1
    assert calls == 2
